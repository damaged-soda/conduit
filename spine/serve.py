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
import ipaddress
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def lan_address(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("LAN 地址必须是 RFC1918 IPv4") from exc
    if not any(address in ipaddress.IPv4Network(network) for network in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise argparse.ArgumentTypeError("LAN 地址必须是 RFC1918 IPv4")
    return str(address)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--nats", required=True, help="骨架统一传入；本程序不连中枢")
    parser.add_argument("--url", default="https://conduit.tail54dd1c.ts.net",
                        help="对外地址，自述给驾驶舱")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="主入口宿主地址；默认回环供 Tailscale Serve 使用")
    parser.add_argument("--lan-bind", type=lan_address,
                        help="可选的额外 LAN IPv4；开放整个无认证服务，仅用于可信局域网")
    parser.add_argument("--port", default="8000")
    parser.add_argument("--image-tag", default="release")
    args = parser.parse_args()
    root = Path(args.root)
    tmp = root / "endpoints.json.tmp"
    endpoints = {"service": args.url}
    compose_args = ["--file", str(HERE / "compose.yaml")]
    if args.lan_bind:
        # Compose 合并 ports 时按 host IP 区分，追加 LAN 映射并保留回环。
        override = root / "compose-lan.json"
        override.write_text(json.dumps({"services": {"conduit": {"ports": [
            f"{args.lan_bind}:{args.port}:8000"
        ]}}}))
        compose_args += ["--file", str(override)]
    tmp.write_text(json.dumps(endpoints))
    tmp.replace(root / "endpoints.json")
    os.environ["CONDUIT_BIND"] = args.bind
    os.environ["CONDUIT_PORT"] = args.port
    os.environ["CONDUIT_IMAGE_TAG"] = args.image_tag
    os.execvp("docker", ["docker", "compose", *compose_args,
                         "up", "--no-color", "--no-build", "--pull", "missing"])


if __name__ == "__main__":
    sys.exit(main())
