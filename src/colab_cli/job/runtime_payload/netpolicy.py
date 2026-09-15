"""Public-destination policy for untrusted job URLs.

Stdlib only: this module is copied to the VM as part of mighty_runtime.
Each request resolves once, rejects any non-public answer, and connects to
an address from that lookup so DNS cannot be rebound between check and
connect. Redirect targets are resolved and checked the same way.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit


class BlockedDestination(Exception):
    """The URL resolved to a non-public destination."""


def _literal_host(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if "%" in host:
        host = host.split("%", 1)[0]
    return host


def address_is_public(value: str) -> bool:
    try:
        address = ipaddress.ip_address(_literal_host(value))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(address.is_global) and not address.is_multicast


def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Return public addresses for `host`, or raise BlockedDestination."""

    host = _literal_host(host)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [info[4][0] for info in infos]
    else:
        addresses = [host]
    if not addresses:
        raise BlockedDestination(f"no addresses for {host}")
    blocked = [ip for ip in addresses if not address_is_public(ip)]
    if blocked:
        raise BlockedDestination(f"non-public address for {host}")
    return addresses


def check_url(url: str) -> tuple[str, str, int]:
    """Require HTTPS and a public destination. Returns host, ip, port."""

    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise BlockedDestination("invalid URL") from error
    if parts.scheme != "https":
        raise BlockedDestination("URL scheme must be https")
    host = parts.hostname
    if not host:
        raise BlockedDestination("URL has no host")
    port = parts.port or 443
    addresses = resolve_public_addresses(host, port)
    return host, addresses[0], port


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, ip: str, port: int, *, server_hostname: str, **kwargs):
        # `port` MUST be passed explicitly, not embedded in `ip` as
        # "host:port" or "[host]:port" and left for HTTPConnection's own
        # `_get_hostport` to sniff out. That sniffer splits on the *last*
        # `:` -- correct for "example.com:8080", wrong for an unbracketed
        # IPv6 literal, whose last `:` precedes its final hextet rather
        # than a port (getaddrinfo can return IPv6 before IPv4, so this
        # class must handle both correctly, always). A hex tail with a
        # letter (e.g. "::cf") raises `InvalidURL: nonnumeric port`; an
        # all-digit tail (e.g. "::12") is worse -- it silently parses as
        # port 12 with a truncated, wrong host, and connects successfully
        # to the wrong place with no error at all. Passing `port`
        # explicitly here skips that sniffing entirely (see
        # `http.client.HTTPConnection._get_hostport`: the whole branch is
        # gated on `if port is None`), so `self.host` ends up exactly
        # `ip`, unmodified, which is also exactly what `socket
        # .create_connection` below needs -- unbracketed, un-reparsed.
        super().__init__(ip, port=port, **kwargs)
        self._server_hostname = server_hostname

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        context = self._context or ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self._server_hostname)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, server_hostname: str, ip: str, port: int, **kwargs):
        super().__init__(**kwargs)
        self._server_hostname = server_hostname
        self._ip = ip
        self._port = port

    def https_open(self, req):
        def builder(*args, **kwargs):
            del args
            kwargs.pop("host", None)
            kwargs.pop("port", None)
            return _PinnedHTTPSConnection(
                self._ip, self._port, server_hostname=self._server_hostname, **kwargs
            )

        return self.do_open(builder, req)


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen_public(req: urllib.request.Request, timeout: float):
    """Open `req` only if every hop is HTTPS to a public address."""

    host, ip, port = check_url(req.full_url)
    parts = urlsplit(req.full_url)
    if ":" in ip and not ip.startswith("["):
        netloc = f"[{ip}]:{port}"
    else:
        netloc = f"{ip}:{port}"
    pinned = urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
    pinned_req = urllib.request.Request(
        pinned,
        data=req.data,
        method=req.get_method(),
        headers=dict(req.header_items()),
    )
    pinned_req.add_header("Host", host)
    opener = urllib.request.build_opener(
        _PublicRedirectHandler(),
        _PinnedHTTPSHandler(host, ip, port),
    )
    return opener.open(pinned_req, timeout=timeout)
