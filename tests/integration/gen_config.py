"""用 render 的**真实产出**生成集成测试用的 mihomo 配置（不是手写配置）。

合成 socks5 节点指向 compose 里的 gost 上游 → build_subscription（纯净版，含 rule#0 兜底直连）→
补上客户端实例设置（mixed-port/controller/allow-lan，订阅本身不含）→ 写 mihomo.generated.yaml。
隔离网无公网，健康检查改指本地 echo-health。
"""

from __future__ import annotations

import pathlib
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))  # repo root，免装也能 import conduit

from conduit.models import AccessId, EndpointId, Node  # noqa: E402
from conduit.render import build_subscription  # noqa: E402


def _node(name: str, server: str) -> Node:
    ep = EndpointId(type="socks5", server=server, port=1080)
    return Node(access_id=AccessId(value=name, endpoint=ep), raw_name=name, params={}, source="it")


def main() -> None:
    # proxy 名 = compose 服务名，故障切换断言可直接 `compose stop <选中节点>`
    cfg = build_subscription([_node("upstream-a", "upstream-a"), _node("upstream-b", "upstream-b")], {}, full=False)
    # 模拟客户端实例设置；放在 base 之后 update，确保覆盖 build_subscription 的骨架默认
    # （尤其 allow-lan: False → True，否则 mihomo 不绑 0.0.0.0、tester 够不到）+ 暴露 controller
    cfg.update({
        "mixed-port": 7890,
        "allow-lan": True,
        "bind-address": "*",
        "external-controller": "0.0.0.0:9090",
    })
    # 隔离网无公网：健康检查指向本地 echo-health（否则上游全被判死，切换测试不准）
    for g in cfg.get("proxy-groups", []):
        g["url"] = "http://echo-health:5678"
        g["expected-status"] = "200"
        g["interval"] = 10
    (HERE / "mihomo.generated.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    print("generated mihomo.generated.yaml from render_subscription output")


if __name__ == "__main__":
    main()
