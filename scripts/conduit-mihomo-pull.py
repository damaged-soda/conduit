#!/usr/bin/env python3
"""conduit-mihomo-pull：从 conduit 订阅 URL 拉取配置，叠本地 overlay，校验后原子安装并激活 mihomo。

定位（ARCHITECTURE.md「送达不在核心流水线里」）：conduit 只生成订阅；每台主机的本地覆盖项
（external-controller / profile / 监听等）由调用方在安装侧叠加。本脚本是那个「通用 hook」：

    pull → deep-merge overlay → 安全检查 → mihomo -t → 备份 + 原子替换 → reload / 重启激活

- 订阅 URL 是 secret：只从 env 文件（默认 `<home>/conduit.env`，建议 0600）里的
  `CONDUIT_SUB_URL=...` 或同名环境变量读取；不进命令行参数，错误信息也不回显
  （`raise ... from None`，异常链里不留含 token 的 cause）。
- overlay 是普通 YAML dict，与订阅输出 deep-merge：dict 递归合并，list / 标量整体替换。
  示例见 examples/mihomo-pull-overlay.example.yaml。合并结果套用与 render.py 相同的安全
  不变量：external-controller（含 TLS 监听）绑非 loopback 且无 secret → 拒绝安装。
- 幂等：合并结果与现有配置一致时直接退出，不备份、不激活。
- fail-closed：拉取 / 解析 / 校验任一步失败都不动现有配置；激活方式在执行前确定，
  激活失败原子回滚（首次安装则删掉新配置）。激活优先走 controller API reload
  （不中断流量），但 mihomo 的 `PUT /configs` 不应用 controller 块（bind / secret /
  CORS）变更 —— 仅当新旧配置的 controller 块完全一致时才允许 reload，否则必须重启；
  无 controller 或 reload 失败时退回 brew services / systemctl 重启；都不可用则必须
  显式 --no-restart。
- 权限：替换保留现有文件 mode；全新安装默认 0600（配置含节点凭据）。
"""

from __future__ import annotations

import argparse
import datetime
import json
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
# 与 conduit/render.py 的 controller 不变量一致：非 loopback 绑定必须带 secret
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# mihomo 启动期才生效、reload 不应用的 API 监听相关键：这些键变了必须重启
_CTRL_KEYS = ("external-controller", "external-controller-tls", "external-controller-cors", "secret")


class PullError(RuntimeError):
    """拉取 / 解析 / 校验 / 安装 / 激活失败。除激活回滚外，任何失败都不应碰现有配置。"""


def deep_merge(base, overlay):
    """dict 递归合并；其余类型（list / 标量 / None）由 overlay 整体替换。"""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    return overlay


def load_sub_url(env_file: Path) -> str:
    """$CONDUIT_SUB_URL > env 文件。都找不到 → 报错。不提供 --url：token 不该进进程参数。"""
    url = os.environ.get("CONDUIT_SUB_URL")
    if not url and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("CONDUIT_SUB_URL="):
                url = line.split("=", 1)[1].strip().strip("'\"")
                break
    if not url:
        raise PullError(f"找不到订阅 URL：设 CONDUIT_SUB_URL 或写进 {env_file}")
    return url


def fetch(url: str) -> str:
    """错误只带异常类型 / HTTP 状态码：URL 含 token，连异常 cause 都不保留。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "conduit-mihomo-pull"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise PullError(f"拉取订阅失败：HTTP {e.code}") from None
    except Exception as e:
        raise PullError(f"拉取订阅失败：{type(e).__name__}") from None


def build_config(sub_text: str, overlay: dict | None) -> dict:
    """解析订阅 → 叠 overlay。输入不像完整配置时 fail-closed。"""
    cfg = yaml.safe_load(sub_text)
    if not isinstance(cfg, dict) or not cfg.get("proxies") or not cfg.get("proxy-groups"):
        raise PullError("订阅内容不像完整 mihomo 配置（缺 proxies / proxy-groups）——拒绝安装")
    if overlay:
        cfg = deep_merge(cfg, overlay)
    return cfg


def check_controller_safety(cfg: dict) -> None:
    """套用 render.py 的 controller 不变量（含 TLS 监听），防 overlay 把 API 绑到非 loopback 裸奔。"""
    for key in ("external-controller", "external-controller-tls"):
        bind = cfg.get(key)
        if not bind:
            continue
        host, _, _ = str(bind).rpartition(":")
        if host not in _LOOPBACK and not cfg.get("secret"):
            raise PullError(f"{key} 绑定非 loopback({bind}) 但无 secret —— 拒绝安装")


def _controller_block(cfg: dict) -> dict:
    """启动期才生效的 API 监听相关键。新旧配置此块不一致时 reload 不可靠，必须重启。"""
    return {k: cfg[k] for k in _CTRL_KEYS if cfg.get(k) is not None}


def _old_controller_block(config: Path) -> dict:
    """读现有配置的 controller 块；不存在 / 解析不出 dict → 按「无」处理（保守走重启）。"""
    try:
        old = yaml.safe_load(config.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return _controller_block(old) if isinstance(old, dict) else {}


def dump_config(cfg: dict) -> str:
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


def install(new_text: str, config: Path) -> tuple[bool, Path | None]:
    """备份 + 原子替换。返回 (是否有变化, 备份路径)；无变化不写盘、不备份。

    已存在的文件保留原 mode（绝不放宽权限）；全新文件 0600。
    """
    old = config.read_text(encoding="utf-8") if config.exists() else None
    if old == new_text:
        return False, None
    mode = (config.stat().st_mode & 0o777) if old is not None else 0o600
    backup = None
    if old is not None:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config.with_name(f"{config.name}.bak.{ts}-pull")
        shutil.copy2(config, backup)
    fd, tmp = tempfile.mkstemp(dir=config.parent, prefix=f".{config.name}.", suffix=".new")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.chmod(tmp, mode)
        os.replace(tmp, config)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True, backup


def restore(backup: Path | None, config: Path) -> None:
    """激活失败时原子回滚磁盘状态（备份保留供排查）；首次安装无备份则删掉新配置。"""
    if backup is None:
        config.unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(dir=config.parent, prefix=f".{config.name}.", suffix=".rollback")
    try:
        with os.fdopen(fd, "wb") as dst, open(backup, "rb") as src:
            shutil.copyfileobj(src, dst)
        shutil.copystat(backup, tmp)
        os.replace(tmp, config)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def api_reload(cfg: dict, config: Path, timeout: int = 15) -> bool:
    """经 external-controller `PUT /configs?force=true` 热加载；不可达 / 失败 → False（调用方兜底）。

    调用方必须保证新旧配置的 controller 块一致（见 _controller_block），否则运行态
    mihomo 的 bind / 凭据可能与本函数从「新配置」读出的一切都对不上。
    """
    bind = cfg.get("external-controller")
    if not bind:
        return False
    req = urllib.request.Request(
        f"http://{bind}/configs?force=true",
        data=json.dumps({"path": str(config)}).encode(),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    if cfg.get("secret"):
        req.add_header("Authorization", f"Bearer {cfg['secret']}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def default_restart_cmd(exists=os.path.exists) -> list[str] | None:
    """macOS brew services（root LaunchDaemon）优先，其次 systemd；都不认识 → None。"""
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


def _run_restart(cmd: list[str]) -> tuple[bool, str]:
    """返回 (成功与否, 输出)。进程不存在等 OSError 也算失败，由调用方回滚。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except OSError as e:
        return False, type(e).__name__


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--home", type=Path, default=None, help="mihomo -d 目录（默认自动探测）")
    p.add_argument("--config", type=Path, default=None, help="目标配置（默认 <home>/config.yaml）")
    p.add_argument("--overlay", type=Path, default=None, help="本地覆盖 YAML，deep-merge 进订阅输出")
    p.add_argument("--env-file", type=Path, default=None, help="含 CONDUIT_SUB_URL 的 env 文件")
    p.add_argument("--mihomo-bin", default=None, help="mihomo 二进制路径（默认自动探测）")
    p.add_argument("--no-restart", action="store_true", help="只安装，不激活 mihomo")
    p.add_argument("--restart-cmd", default=None, help="自定义重启命令（默认自动探测）")
    args = p.parse_args(argv)

    args.home = args.home or _default_home()
    args.config = (args.config or args.home / "config.yaml").resolve()  # reload 需要绝对路径
    args.env_file = args.env_file or args.home / "conduit.env"
    mihomo_bin = args.mihomo_bin or _default_mihomo_bin()

    overlay = None
    if args.overlay:
        overlay = yaml.safe_load(args.overlay.read_text(encoding="utf-8"))
        if overlay is not None and not isinstance(overlay, dict):
            raise PullError(f"overlay 必须是 YAML dict：{args.overlay}")

    cfg = build_config(fetch(load_sub_url(args.env_file)), overlay)
    check_controller_safety(cfg)
    new_text = dump_config(cfg)
    validate(mihomo_bin, args.home, new_text)

    # 激活方式在执行前确定。reload 只在「controller 块新旧一致」时可靠（bind / secret /
    # CORS 启动期才生效，且运行态凭据来自旧配置）；否则必须拿到重启命令。
    # 两者皆无且未显式 --no-restart → 不安装直接失败（避免磁盘态 / 运行态分叉）。
    old_ctrl = _old_controller_block(args.config)
    new_ctrl = _controller_block(cfg)
    can_reload = bool(new_ctrl.get("external-controller")) and old_ctrl == new_ctrl
    restart_cmd = None if args.no_restart else (
        shlex.split(args.restart_cmd) if args.restart_cmd else default_restart_cmd())
    if not args.no_restart and not can_reload and restart_cmd is None:
        raise PullError("controller 块有变化（或无 controller）且探测不到重启命令；"
                        "确认激活方式或用 --no-restart 显式只安装")

    changed, backup = install(new_text, args.config)
    if not changed:
        print("配置无变化，跳过安装与激活")
        return 0
    print(f"已安装 {args.config}")

    if args.no_restart:
        print("--no-restart：跳过激活，记得手动 reload / restart mihomo")
        return 0
    if can_reload and api_reload(cfg, args.config):
        print("已通过 controller API reload 生效")
        return 0
    if restart_cmd is not None:
        ok, detail = _run_restart(restart_cmd)
        if ok:
            print(f"已重启 mihomo：{' '.join(restart_cmd)}")
            return 0
        restore(backup, args.config)
        _run_restart(restart_cmd)  # 尽力把服务恢复到旧配置对应的状态
        raise PullError(f"重启失败，已回滚旧配置：{' '.join(restart_cmd)}\n{detail}")
    restore(backup, args.config)
    raise PullError("controller reload 失败且无重启命令 —— 已回滚旧配置，请检查 mihomo 状态")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PullError as e:
        print(f"conduit-mihomo-pull: {e}", file=sys.stderr)
        sys.exit(1)
