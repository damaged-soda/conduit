"""服务侧的网络抓取（impure I/O）。core 仍纯函数：service 抓内容，再交给 conduit.normalize。

⚠️ 自举注意：抓订阅可能需要能出网（甚至翻墙）。当 rig 自身出网走它生成的 mihomo 时，
要给这条抓取路径留直连/旁路（direct-list）；订阅地址抓不到时退回「文件导入」。
"""

from __future__ import annotations

import httpx


def fetch_url(url: str, timeout: float = 20.0) -> str:
    """GET 一个订阅 URL，返回正文（跟随重定向）。失败抛 httpx 异常，由调用方映射成 5xx。"""
    r = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"user-agent": "conduit/0.0"})
    r.raise_for_status()
    return r.text
