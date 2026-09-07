#!/usr/bin/env python3
"""conduit 服务质料：spine 是唯一保活者，本程序就是前台的 compose up。

写下自述地址后 exec 成 docker compose 客户端——进程生命周期 = 服务生命周期，
SIGTERM 由 compose 客户端优雅停容器，退出即由骨架按常驻语义重启。compose
项目名沿用旧现场（conduit-service），起动即收养既有容器；restart 策略为 no，
容器不自我复活。`--no-build --pull missing` 是生产纪律：禁构建防 Compose 吞
拉取错误回退本机构建，镜像本地在则不碰 registry、缺才拉且拉失败 fail loud。
conduit 镜像是公开包零认证拉，不需要凭据目录。docker engine 属 OS 层底座，
不在登记范围；tailnet HTTPS 仍由宿主 Tailscale Serve（svc:conduit → 本机回环
端口）承担，本质料不改接线。--nats 收下不用（心跳由骨架代发）。
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--nats", required=True, help="骨架统一传入；本程序不连中枢")
    parser.add_argument("--url", default="https://conduit.tail54dd1c.ts.net",
                        help="对外地址，自述给驾驶舱")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="容器端口映射的宿主侧地址；serve 后端只认回环")
    parser.add_argument("--port", default="8000")
    parser.add_argument("--image-tag", default="release")
    args = parser.parse_args()
    root = Path(args.root)
    tmp = root / "endpoints.json.tmp"
    tmp.write_text(json.dumps({"service": args.url}))
    tmp.replace(root / "endpoints.json")
    os.environ["CONDUIT_BIND"] = args.bind
    os.environ["CONDUIT_PORT"] = args.port
    os.environ["CONDUIT_IMAGE_TAG"] = args.image_tag
    os.execvp("docker", ["docker", "compose",
                         "--file", str(HERE / "compose.yaml"),
                         "up", "--no-color", "--no-build", "--pull", "missing"])


if __name__ == "__main__":
    sys.exit(main())
