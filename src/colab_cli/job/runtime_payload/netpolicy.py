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
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit


class BlockedDestination(Exception):
    """The URL resolved to a non-public destination."""


class HTTPStatusError(Exception):
    """A request got a non-2xx response.

    `body` holds the first bytes of the response body. `send_error` is set
    when the server answered before the request body was fully sent.
    """

    def __init__(self, status, reason, body, send_error=None):
        message = f"HTTP {status} {reason}".rstrip()
        if send_error is not None:
            message += f" (upload cut short: {type(send_error).__name__}: {send_error})"
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.body = body
        self.send_error = send_error


class UploadCutShort(Exception):
    """The request body could not be fully sent, and the server's response
    does not explain why. The message names the send error and what
    happened when the response was read."""

    def __init__(self, send_error, after):
        super().__init__(f"{type(send_error).__name__}: {send_error}; {after}")
        self.send_error = send_error


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
        raise BlockedDestination(
            f"non-public address for {host}: {', '.join(blocked)}"
        )
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


def _public_connection(url: str, timeout: float):
    host, ip, port = check_url(url)
    return _PinnedHTTPSConnection(ip, port, server_hostname=host, timeout=timeout), host


def put_public(url, body, length, headers, timeout, body_limit=300):
    """PUT `length` bytes read from `body` to a public HTTPS destination.

    Uses `http.client` directly rather than urllib: a server that rejects
    an upload from its headers (a proxy's size limit, an expired
    signature) answers and closes while the body is still being sent.
    urllib then raises only the broken pipe. Reading the response after
    the send fails recovers the server's status and the first
    `body_limit` bytes of its body, raised as `HTTPStatusError`. A PUT is
    never redirected, so there are no further hops to check.
    """

    connection, host = _public_connection(url, timeout)
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        connection.putrequest("PUT", target, skip_host=True, skip_accept_encoding=True)
        # The headers urllib sends: a request without a User-Agent can be
        # challenged by a CDN in front of the destination.
        connection.putheader("Host", host)
        connection.putheader("User-Agent", "Python-urllib/%d.%d" % sys.version_info[:2])
        connection.putheader("Accept-Encoding", "identity")
        connection.putheader("Connection", "close")
        connection.putheader("Content-Length", str(length))
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        send_error = None
        try:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
        except OSError as error:
            send_error = error
        try:
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as response_error:
            if send_error is None:
                raise
            raise UploadCutShort(
                send_error,
                "then reading the response failed: "
                f"{type(response_error).__name__}: {response_error}",
            ) from response_error
        excerpt = response.read(body_limit)
        if 200 <= response.status < 300:
            if send_error is not None:
                raise UploadCutShort(
                    send_error,
                    f"the server answered HTTP {response.status} {response.reason}",
                )
            return response.status
        raise HTTPStatusError(response.status, response.reason, excerpt, send_error)
    finally:
        connection.close()
