#!/usr/bin/env python3
"""conduit-mihomo-pull：从 conduit 订阅 URL 拉取配置，叠本地 overlay，校验后原子安装并按需重启 mihomo。

定位（ARCHITECTURE.md「送达不在核心流水线里」）：conduit 只生成订阅；每台主机的本地覆盖项
（external-controller / profile / 监听等）由调用方在安装侧叠加。本脚本是那个「通用 hook」：

    pull → deep-merge overlay → mihomo -t 校验 → 备份 + 原子替换 → 内容变化时重启

- 订阅 URL 是 secret：从 env 文件（默认 `<home>/conduit.env`，建议 0600）里的
  `CONDUIT_SUB_URL=...` 或同名环境变量读取；不出现在日志和进程参数里。
- overlay 是普通 YAML dict，与订阅输出 deep-merge：dict 递归合并，list / 标量整体替换。
  示例见 examples/mihomo-pull-overlay.example.yaml。
- 幂等：合并结果与现有配置一致时直接退出，不备份、不重启。
- fail-closed：拉取 / 解析 / 校验任一步失败都不动现有配置。
"""

from __future__ import annotations

import argparse
import datetime
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

_FETCH_TIMEOUT = 30
_HOME_CANDIDATES = ("/opt/homebrew/etc/mihomo", "/etc/mihomo")
_MIHOMO_CANDIDATES = ("/opt/homebrew/opt/mihomo/bin/mihomo", "/usr/local/bin/mihomo")


class PullError(RuntimeError):
    """拉取 / 解析 / 校验 / 安装失败。任何一步失败都不应碰现有配置。"""


def deep_merge(base, overlay):
    """dict 递归合并；其余类型（list / 标量 / None）由 overlay 整体替换。"""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    return overlay


def load_sub_url(args: argparse.Namespace) -> str:
    """优先级：--url > $CONDUIT_SUB_URL > env 文件。都找不到 → 报错。"""
    url = args.url or os.environ.get("CONDUIT_SUB_URL")
    if not url and args.env_file.exists():
        for line in args.env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("CONDUIT_SUB_URL="):
                url = line.split("=", 1)[1].strip().strip("'\"")
                break
    if not url:
        raise PullError(f"找不到订阅 URL：设 CONDUIT_SUB_URL 或写进 {args.env_file}")
    return url


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "conduit-mihomo-pull"})
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:  # 网络 / HTTP 错误统一 fail-closed；不回显 URL（secret）
        raise PullError(f"拉取订阅失败：{e}") from e


def build_config(sub_text: str, overlay: dict | None) -> str:
    """解析订阅 → 叠 overlay → 导出 YAML。输入不像完整配置时 fail-closed。"""
    cfg = yaml.safe_load(sub_text)
    if not isinstance(cfg, dict) or not cfg.get("proxies") or not cfg.get("proxy-groups"):
        raise PullError("订阅内容不像完整 mihomo 配置（缺 proxies / proxy-groups）——拒绝安装")
    if overlay:
        cfg = deep_merge(cfg, overlay)
    return yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)


def validate(mihomo_bin: str, home: Path, config_text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".conduit-pull-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(config_text)
        proc = subprocess.run(
            [mihomo_bin, "-d", str(home), "-f", tmp, "-t"],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise PullError(f"mihomo -t 校验失败：\n{(proc.stdout + proc.stderr).strip()}")
    finally:
        Path(tmp).unlink(missing_ok=True)


def install(new_text: str, config: Path) -> bool:
    """备份 + 原子替换。返回是否有变化；无变化不写盘、不备份。"""
    old = config.read_text(encoding="utf-8") if config.exists() else None
    if old == new_text:
        return False
    if old is not None:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(config, config.with_name(f"{config.name}.bak.{ts}-pull"))
    fd, tmp = tempfile.mkstemp(dir=config.parent, prefix=f".{config.name}.", suffix=".new")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, config)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def default_restart_cmd(exists=os.path.exists) -> list[str] | None:
    """macOS brew services（root LaunchDaemon）优先，其次 systemd；都不认识 → None（只装不重启）。"""
    if exists("/Library/LaunchDaemons/homebrew.mxcl.mihomo.plist"):
        return ["sudo", "-n", "brew", "services", "restart", "mihomo"]
    if exists("/etc/systemd/system/mihomo.service"):
        return ["sudo", "-n", "systemctl", "restart", "mihomo"]
    return None


def _default_home() -> Path:
    env = os.environ.get("CONDUIT_MIHOMO_HOME")
    if env:
        return Path(env)
    for c in _HOME_CANDIDATES:
        if Path(c).is_dir():
            return Path(c)
    raise PullError(f"找不到 mihomo home（{_HOME_CANDIDATES}），用 --home 指定")


def _default_mihomo_bin() -> str:
    found = shutil.which("mihomo")
    if found:
        return found
    for c in _MIHOMO_CANDIDATES:
        if Path(c).is_file():
            return c
    raise PullError("找不到 mihomo 二进制，用 --mihomo-bin 指定")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--home", type=Path, default=None, help="mihomo -d 目录（默认自动探测）")
    p.add_argument("--config", type=Path, default=None, help="目标配置（默认 <home>/config.yaml）")
    p.add_argument("--overlay", type=Path, default=None, help="本地覆盖 YAML，deep-merge 进订阅输出")
    p.add_argument("--env-file", type=Path, default=None, help="含 CONDUIT_SUB_URL 的 env 文件")
    p.add_argument("--url", default=None, help="订阅 URL（secret；优先用 env 文件，别上命令行）")
    p.add_argument("--mihomo-bin", default=None, help="mihomo 二进制路径（默认自动探测）")
    p.add_argument("--no-restart", action="store_true", help="只安装，不重启 mihomo")
    p.add_argument("--restart-cmd", default=None, help="自定义重启命令（默认自动探测）")
    args = p.parse_args(argv)

    args.home = args.home or _default_home()
    args.config = args.config or args.home / "config.yaml"
    args.env_file = args.env_file or args.home / "conduit.env"
    mihomo_bin = args.mihomo_bin or _default_mihomo_bin()

    overlay = None
    if args.overlay:
        overlay = yaml.safe_load(args.overlay.read_text(encoding="utf-8"))
        if overlay is not None and not isinstance(overlay, dict):
            raise PullError(f"overlay 必须是 YAML dict：{args.overlay}")

    new_text = build_config(fetch(load_sub_url(args)), overlay)
    validate(mihomo_bin, args.home, new_text)
    if not install(new_text, args.config):
        print("配置无变化，跳过安装与重启")
        return 0
    print(f"已安装 {args.config}")

    if args.no_restart:
        print("--no-restart：跳过重启，记得手动 reload / restart mihomo")
        return 0
    cmd = shlex.split(args.restart_cmd) if args.restart_cmd else default_restart_cmd()
    if cmd is None:
        print("未识别 init 系统，跳过重启，请手动重启 mihomo", file=sys.stderr)
        return 0
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PullError(f"配置已安装但重启失败：{' '.join(cmd)}\n{(proc.stdout + proc.stderr).strip()}")
    print(f"已重启 mihomo：{' '.join(cmd)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PullError as e:
        print(f"conduit-mihomo-pull: {e}", file=sys.stderr)
        sys.exit(1)
