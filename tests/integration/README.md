# 集成测试台（Docker，TESTING.md 第 2 层）

**无外网**的隔离 Linux 网络里跑 mihomo + 可 kill 的 mock 上游，经代理端口 / API 验证**路由**与**故障切换**。先不开 TUN。

⚠️ Docker on Mac 是 Linux VM —— 这层验的是 **Linux/rig 路径**；macOS 原生 TUN/utun 要上 MBA 实机（TESTING.md 第 3 层）。

## 组成
- `compose.yaml`：mihomo（被测）+ upstream-a/b（gost，可 kill）+ echo-health（本地健康目标）+ echo-proxied/echo-direct（标记走了哪条路）+ tester（curl）。网络 `internal: true`，**真隔离无公网**。
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
- 测 TUN / 私有网旁路：mihomo 服务加 `cap_add: [NET_ADMIN]` + `/dev/net/tun`，并在同 netns 起那张私有网（见 TESTING.md）。
