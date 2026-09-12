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
