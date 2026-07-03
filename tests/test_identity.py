from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from conduit.identity import access_id  # noqa: E402


def _proxy(**params):
    return {
        "name": "node",
        "type": "ss",
        "server": "Example.com",
        "port": 8388,
        "cipher": "2022-blake3-aes-256-gcm",
        "password": "p",
        **params,
    }


def test_access_id_ignores_client_capability_flags():
    base = access_id(_proxy()).value

    assert access_id(_proxy(udp=True)).value == base
    assert access_id(_proxy(udp=False, tfo=True)).value == base


def test_access_id_still_tracks_connection_parameters():
    assert access_id(_proxy(password="p1")).value != access_id(_proxy(password="p2")).value
