# 集成测试台（Docker，TESTING.md 第 2 层）

**无外网**的隔离 Linux 网络里跑 mihomo + 可 kill 的 mock 上游，经代理端口 / API 验证**路由**与**故障切换**。先不开 TUN。

⚠️ Docker on Mac 是 Linux VM —— 这层验的是 **Linux/rig 路径**；macOS 原生 TUN/utun 要上 MBA 实机（TESTING.md 第 3 层）。

## 组成
- `compose.yaml`：mihomo + upstream-a/b（gost，可 kill）+ echo-health + echo-proxied/echo-direct + tester。三张 `internal` 网络做**结构化证明**：echo-proxied 只在 backnet（mihomo 必须经 upstream → 证明走代理）、echo-direct 只在 directnet（upstream 到不了 → 证明走直连）。全程无公网。
- `mihomo.proxy-only.yaml`：被测的 mihomo 配置（无 TUN，上游指向 compose mock 代理，健康检查打 echo-health）。**临时手写，后续换成 render() 产物**。
- `run.sh`：测试流程（readiness → 路由 → 故障切换）。**首版断言，未在本机实跑验证**——在 MBA 上跑通后固化。

## 跑
```
./run.sh
```

## 待补
- 用 render() 真实产物替换手写配置。
- 量化「kill→恢复」耗时对照 SLO；用 `/proxies/PROXY` 确认确实切换；长连接确认 chain=DIRECT。
- 镜像固定 tag/digest；确认 `ginuerzh/gost` 版本（或换官方维护镜像）。
- 测 TUN：mihomo 服务加 `cap_add: [NET_ADMIN]` + `/dev/net/tun`，留自己的 netns。
- **rule#0 私有网旁路变体**（最关键）：privileged 容器跑真 overlay（如 tailscale，join 一次性 headscale）+ 同 netns mihomo-TUN，断言 TUN 开着时 peer 仍可达。设计见 TESTING.md「关键测试」。
