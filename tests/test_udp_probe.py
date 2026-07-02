from __future__ import annotations

import stat
import struct

import pytest

from conduit.ingest import normalize
from service.udp_probe import (
    _dns_query_packet,
    _dns_response_ok,
    _mihomo_config,
    _socks_addr,
    _socks_udp_payload,
    _write_config,
)


def test_socks_addr_encodes_ipv4_ipv6_and_domain():
    assert _socks_addr("1.2.3.4", 53) == b"\x01\x01\x02\x03\x04\x005"
    assert _socks_addr("2001:db8::1", 853).startswith(b"\x04")
    assert _socks_addr("example.com", 853) == b"\x03\x0bexample.com\x03U"


def test_socks_udp_payload_parses_address_variants():
    assert _socks_udp_payload(b"\x00\x00\x00\x01\x01\x02\x03\x04\x005abc") == b"abc"
    assert _socks_udp_payload(b"\x00\x00\x00\x03\x0bexample.com\x005abc") == b"abc"
    assert _socks_udp_payload(b"\x00\x00\x00\x04" + bytes(16) + b"\x005abc") == b"abc"


def test_socks_udp_payload_rejects_bad_headers():
    with pytest.raises(OSError):
        _socks_udp_payload(b"\x00\x01\x00\x01\x01\x02\x03\x04\x005abc")
    with pytest.raises(OSError):
        _socks_udp_payload(b"\x00\x00\x00\x09\x005abc")


def test_dns_query_packet_and_response_validation():
    packet, query_id = _dns_query_packet("cloudflare.com")
    assert packet[:2] == query_id
    assert packet[4:6] == b"\x00\x01"

    nxdomain_response = query_id + struct.pack("!HHHHH", 0x8183, 1, 0, 0, 0)
    assert _dns_response_ok(nxdomain_response, query_id)[0] is True
    assert _dns_response_ok(nxdomain_response, b"\x00\x00")[0] is False
    no_qr_response = query_id + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0)
    assert _dns_response_ok(no_qr_response, query_id)[0] is False


def test_probe_config_whitelists_proxy_fields_and_forces_udp():
    raw = (
        "proxies:\n"
        "  - {name: t, type: trojan, server: s.example.com, port: 443, password: p, "
        "sni: s.example.com, udp: false, dialer-proxy: evil, interface-name: en0}\n"
    )
    node = normalize(raw, "auto", "probe")[0]

    proxy = _mihomo_config(node, 19093)["proxies"][0]
    assert proxy["password"] == "p"
    assert proxy["sni"] == "s.example.com"
    assert proxy["udp"] is True
    assert "dialer-proxy" not in proxy
    assert "interface-name" not in proxy


def test_write_config_uses_owner_only_permissions(tmp_path):
    path = tmp_path / "config.yaml"
    _write_config(path, {"mode": "rule"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
