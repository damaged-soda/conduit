"""Short-lived, isolated UDP probing through a single mihomo node."""

from __future__ import annotations

import contextlib
import ipaddress
import os
import random
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from conduit.models import Node

_CORE_KEYS = {"name", "type", "server", "port"}
_COMMON_PROXY_KEYS = {
    "udp",
    "sni",
    "servername",
    "skip-cert-verify",
    "fingerprint",
    "client-fingerprint",
    "alpn",
    "network",
    "ws-opts",
    "grpc-opts",
    "h2-opts",
    "tls",
    "flow",
}
_PROXY_KEYS_BY_TYPE = {
    "ss": {"cipher", "password", "plugin", "plugin-opts"},
    "vmess": {"uuid", "alterId", "cipher"},
    "trojan": {"password", "reality-opts", "ss-opts"},
    "vless": {"uuid", "encryption", "packet-encoding", "reality-opts"},
    "hysteria": {"auth-str", "protocol", "up", "down", "obfs"},
    "hysteria2": {"password", "ports", "hop-interval", "up", "down", "obfs", "obfs-password"},
    "hy2": {"password", "ports", "hop-interval", "up", "down", "obfs", "obfs-password"},
}
_UDP_DEFAULT_TARGET = "1.1.1.1"
_UDP_DEFAULT_PORT = 53
_UDP_DEFAULT_QUERY = "cloudflare.com"
_STARTUP_TIMEOUT = 5.0
_DNS_TIMEOUT = 5.0


def probe_udp(node: Node) -> dict[str, Any]:
    """Probe UDP by forcing one node through a temporary localhost-only mihomo."""
    started = time.monotonic()
    binary = _mihomo_binary()
    if not binary:
        return _result(
            "unavailable",
            None,
            "mihomo binary not found; set CONDUIT_MIHOMO_BIN",
            started,
        )

    target = (
        os.environ.get("CONDUIT_UDP_PROBE_DNS_HOST", _UDP_DEFAULT_TARGET).strip()
        or _UDP_DEFAULT_TARGET
    )
    query = (
        os.environ.get("CONDUIT_UDP_PROBE_DOMAIN", _UDP_DEFAULT_QUERY).strip().rstrip(".")
        or _UDP_DEFAULT_QUERY
    )
    port = _free_local_port()

    with tempfile.TemporaryDirectory(prefix="conduit-udp-probe-") as td:
        workdir = Path(td)
        config = workdir / "config.yaml"
        _write_config(config, _mihomo_config(node, port))
        proc = subprocess.Popen(
            [binary, "-f", str(config), "-d", str(workdir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            ready = _wait_for_port(proc, port, _STARTUP_TIMEOUT)
            if ready is not None:
                status, output = ready
                return _result(status, None, output, started, target, _UDP_DEFAULT_PORT)

            try:
                ok, message = _dns_query_via_socks(
                    "127.0.0.1", port, target, _UDP_DEFAULT_PORT, query, _DNS_TIMEOUT
                )
            except Exception as e:
                ok, message = False, f"UDP probe failed: {type(e).__name__}"
            return _result(
                "supported" if ok else "failed",
                ok,
                message,
                started,
                target,
                _UDP_DEFAULT_PORT,
            )
        finally:
            _stop(proc)


def _mihomo_binary() -> str | None:
    configured = os.environ.get("CONDUIT_MIHOMO_BIN", "").strip()
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    for name in ("mihomo", "clash-meta", "clash"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _mihomo_config(node: Node, port: int) -> dict:
    proxy = _node_to_proxy(node)
    proxy["name"] = "udp-probe"
    proxy["udp"] = True
    return {
        "socks-port": port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": True,
        "proxies": [proxy],
        "proxy-groups": [{"name": "UDP-PROBE", "type": "select", "proxies": ["udp-probe"]}],
        "rules": ["MATCH,UDP-PROBE"],
    }


def _node_to_proxy(node: Node) -> dict:
    ep = node.access_id.endpoint
    allowed = _COMMON_PROXY_KEYS | _PROXY_KEYS_BY_TYPE.get(ep.type.lower(), set())
    safe = {k: v for k, v in (node.params or {}).items() if k in allowed and k not in _CORE_KEYS}
    return {
        "name": node.raw_name or "node",
        "type": ep.type,
        "server": ep.server,
        "port": ep.port,
        **safe,
    }


def _write_config(path: Path, config: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(proc: subprocess.Popen, port: int, timeout: float) -> tuple[str, str] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            _collect(proc)
            return "unavailable", f"mihomo exited before becoming ready with code {code}"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return None
        except OSError:
            time.sleep(0.1)
    return "unavailable", "mihomo did not become ready before timeout"


def _dns_query_via_socks(
    socks_host: str, socks_port: int, dns_host: str, dns_port: int, query: str, timeout: float
) -> tuple[bool, str]:
    packet, query_id = _dns_query_packet(query)
    with socket.create_connection((socks_host, socks_port), timeout=timeout) as tcp:
        tcp.settimeout(timeout)
        tcp.sendall(b"\x05\x01\x00")
        if _read_exact(tcp, 2) != b"\x05\x00":
            return False, "SOCKS handshake rejected"

        tcp.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        head = _read_exact(tcp, 4)
        if head[1] != 0:
            return False, f"SOCKS UDP associate failed: {head[1]}"
        relay_host, relay_port = _read_socks_addr(tcp, head[3])
        if relay_host in ("0.0.0.0", "::"):
            relay_host = socks_host

        family = socket.AF_INET6 if ":" in relay_host else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as udp:
            udp.settimeout(timeout)
            udp.sendto(
                b"\x00\x00\x00" + _socks_addr(dns_host, dns_port) + packet,
                (relay_host, relay_port),
            )
            data, _ = udp.recvfrom(4096)

    payload = _socks_udp_payload(data)
    return _dns_response_ok(payload, query_id, dns_host, dns_port)


def _dns_response_ok(
    payload: bytes,
    query_id: bytes,
    dns_host: str = _UDP_DEFAULT_TARGET,
    dns_port: int = _UDP_DEFAULT_PORT,
) -> tuple[bool, str]:
    if len(payload) < 12 or payload[:2] != query_id:
        return False, "UDP response did not match DNS probe"
    flags, qdcount = struct.unpack("!HH", payload[2:6])
    rcode = flags & 0x000F
    if not flags & 0x8000:
        return False, "DNS probe response was not marked as a response"
    if qdcount != 1:
        return False, "DNS probe response question count did not match"
    return True, f"UDP DNS probe received response from {dns_host}:{dns_port} rcode={rcode}"


def _dns_query_packet(name: str) -> tuple[bytes, bytes]:
    query_id = (
        random.randbytes(2)
        if hasattr(random, "randbytes")
        else random.getrandbits(16).to_bytes(2, "big")
    )
    labels = name.split(".")
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"
    header = query_id + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    return header + qname + b"\x00\x01\x00\x01", query_id


def _socks_addr(host: str, port: int) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raw = host.encode("idna")
        if len(raw) > 255:
            raise ValueError("SOCKS domain is too long")
        return b"\x03" + bytes([len(raw)]) + raw + struct.pack("!H", port)
    if ip.version == 4:
        return b"\x01" + ip.packed + struct.pack("!H", port)
    return b"\x04" + ip.packed + struct.pack("!H", port)


def _read_socks_addr(sock: socket.socket, atyp: int) -> tuple[str, int]:
    if atyp == 1:
        host = socket.inet_ntop(socket.AF_INET, _read_exact(sock, 4))
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, _read_exact(sock, 16))
    elif atyp == 3:
        size = _read_exact(sock, 1)[0]
        host = _read_exact(sock, size).decode("idna")
    else:
        raise OSError(f"unsupported SOCKS address type: {atyp}")
    port = struct.unpack("!H", _read_exact(sock, 2))[0]
    return host, port


def _socks_udp_payload(data: bytes) -> bytes:
    if len(data) < 4 or data[:2] != b"\x00\x00" or data[2] != 0:
        raise OSError("invalid SOCKS UDP response header")
    atyp = data[3]
    offset = 4
    if atyp == 1:
        offset += 4
    elif atyp == 4:
        offset += 16
    elif atyp == 3:
        if len(data) < 5:
            raise OSError("short SOCKS UDP domain header")
        offset += 1 + data[4]
    else:
        raise OSError(f"unsupported SOCKS UDP address type: {atyp}")
    return data[offset + 2 :]


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("short read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=2)
        return
    with contextlib.suppress(Exception):
        proc.kill()


def _collect(proc: subprocess.Popen) -> str:
    try:
        out, _ = proc.communicate(timeout=1)
        return out or ""
    except subprocess.TimeoutExpired:
        return ""


def _result(
    status: str,
    ok: bool | None,
    message: str,
    started: float,
    target: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "ok": ok,
        "message": message,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if target and port:
        out["target"] = f"{target}:{port}"
    return out
