# 集成测试台（Docker，TESTING.md 第 2 层）

Linux 隔离网络里跑 mihomo + 可 kill 的 mock 上游，经代理端口 / API 验证**路由**与**故障切换**。先不开 TUN。

⚠️ Docker on Mac 是 Linux VM —— 这层验的是 **Linux/rig 路径**；macOS 原生 TUN/utun 要上 MBA 实机（TESTING.md 第 3 层）。

## 组成
- `compose.yaml`：mihomo（被测）+ upstream-a/b（gost，可 kill）+ echo-proxied/echo-direct（标记走了哪条路）+ tester（curl）。
- `mihomo.proxy-only.yaml`：被测的 mihomo 配置（无 TUN，上游指向 compose 里的 mock 代理）。**临时手写，后续换成 render() 产物**。
- `run.sh`：测试流程骨架（up → 路由/切换断言 → down）。多数断言是 TODO。

## 跑
```
./run.sh
```

## 待补
- 用 render() 真实产物替换手写配置。
- 断言驱动：curl + external-controller `/connections` 核对 chain；kill 上游量切换耗时。
- 测 TUN / 私有网旁路：给 mihomo 服务加 `cap_add: [NET_ADMIN]` + `/dev/net/tun`，并在同 netns 起那张私有网（见 TESTING.md）。
