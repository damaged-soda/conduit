"""服务侧的网络抓取（impure I/O）。core 仍纯函数：service 抓内容，再交给 conduit.normalize。

⚠️ 自举注意：抓订阅可能需要能出网（甚至翻墙）。当 rig 自身出网走它生成的 mihomo 时，
要给这条抓取路径留直连/旁路（direct-list）；订阅地址抓不到时退回「文件导入」。
"""

from __future__ import annotations

import httpx

_MAX_BYTES = 5 * 1024 * 1024  # 订阅响应大小上限（5 MiB）：防坏 URL 撑爆内存 / DB


def fetch_url(url: str, timeout: float = 20.0) -> str:
    """GET 一个订阅 URL，返回正文（跟随重定向，限大小）。失败抛异常，由调用方映射成 5xx。"""
    chunks: list[bytes] = []
    total = 0
    headers = {"user-agent": "mihomo/1.18.3 conduit/0.0"}
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True, headers=headers) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            total += len(chunk)
            if total > _MAX_BYTES:
                raise ValueError("订阅响应超过大小上限")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")
