from __future__ import annotations

import pytest

from colab_cli.job.runtime_payload.netpolicy import (
    BlockedDestination,
    address_is_public,
    check_url,
    resolve_public_addresses,
)


def test_literal_link_local_and_loopback_are_blocked():
    assert address_is_public("8.8.8.8")
    assert not address_is_public("10.0.0.1")
    assert not address_is_public("127.0.0.1")
    assert not address_is_public("169.254.1.1")
    assert not address_is_public("fe80::1")
    assert not address_is_public("::1")
    assert not address_is_public("fc00::1")
    assert not address_is_public("::ffff:10.0.0.1")


def test_resolve_rejects_mixed_public_and_private_answers():
    with pytest.raises(BlockedDestination):
        resolve_public_addresses("private.test", 443)
    with pytest.raises(BlockedDestination):
        resolve_public_addresses("mixed.test", 443)
    assert resolve_public_addresses("storage.example.test", 443) == ["8.8.8.8"]


def test_https_is_required_and_destination_is_pinned_to_a_public_ip():
    host, ip, port = check_url("https://storage.example.test/obj")
    assert host == "storage.example.test"
    assert ip == "8.8.8.8"
    assert port == 443
    with pytest.raises(BlockedDestination):
        check_url("http://storage.example.test/obj")
    with pytest.raises(BlockedDestination):
        check_url("https://[fe80::1]/obj")


def test_pinned_connection_handles_unbracketed_ipv6_and_nondefault_port():
    """`getaddrinfo` can return an IPv6 answer first (e.g. `2a00:1450:...`).

    `http.client.HTTPConnection`'s own `_get_hostport` splits `host` on its
    *last* `:` to sniff an embedded port when `port` isn't passed
    explicitly -- which misreads an unbracketed IPv6 literal's trailing
    hextet as the port (`InvalidURL: nonnumeric port` for a hex tail with
    a letter, or worse, a silently wrong port for an all-digit tail like
    `::12`). The fix is to always pass `port` explicitly so that internal
    sniffing never runs, not to bracket the literal (bracketing alone
    would dodge the misparse but silently drop any non-default port, since
    `_get_hostport` falls back to `self.default_port` whenever no port
    suffix is present).
    """
    from colab_cli.job.runtime_payload.netpolicy import _PinnedHTTPSConnection

    conn = _PinnedHTTPSConnection(
        "2a00:1450:4009:c08::cf", 8443, server_hostname="example.com"
    )

    assert conn.host == "2a00:1450:4009:c08::cf"
    assert conn.port == 8443
