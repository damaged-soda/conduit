from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from service import fetch  # noqa: E402


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield b"proxies: []\n"


def test_fetch_url_prefers_clash_meta_user_agent(monkeypatch):
    seen = {}

    def fake_stream(method, url, timeout, follow_redirects, headers):
        seen.update(
            {
                "method": method,
                "url": url,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
                "headers": headers,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr(fetch.httpx, "stream", fake_stream)

    assert fetch.fetch_url("https://example.test/sub") == "proxies: []\n"
    assert seen["method"] == "GET"
    assert seen["follow_redirects"] is True
    assert seen["headers"]["user-agent"] == "clash.meta"
