# 集成测试台（Docker，本地 + GitHub PR CI）

用 **render 的真实产出** 跑 mihomo，在隔离网络里断言**路由语义**（不是手写配置、也不只看 `mihomo -t` 合法性）：
- 私网 IP（`172.28.0.5`）→ **直连**（命中 render 的 rule#0 兜底）；
- 域名 → **代理**（经 upstream）；
- kill upstream → **切换**。

代理模式（不开 TUN），GitHub runner / 本地 Docker 都能跑。

## 组成
- `gen_config.py`：合成 socks5 节点指向 gost 上游 → `build_subscription`（含 rule#0 兜底）→ 补客户端实例设置（mixed-port/controller，订阅本身不含）→ `mihomo.generated.yaml`（gitignored）；健康检查指本地 echo-health（隔离网无公网）。
- `compose.yaml`：3 张 `internal` 网络做结构化证明 —— echo-proxied 只在 backnet（只能经 upstream 到 = 走代理），echo-direct `172.28.0.5` 只在 directnet（upstream 够不到 = 只能直连）。
- `run.sh`：gen → up → 三条断言 → down。

## 跑
```
pip install -e .            # 让 python3 能 import conduit + pyyaml
./run.sh                    # 本地可 PYTHON=/path/to/venv/bin/python ./run.sh
```
CI 见 `.github/workflows/ci.yml` 的 `integration` job（PR 触发）。

⚠️ 代理模式验的是**规则语义**（含 rule#0）的 Linux 路径；macOS 原生 TUN / tailscale 旁路的真实测试要 privileged + 同 netns 起 tailscale，留 later（见 ../../TESTING.md）。
