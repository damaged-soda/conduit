# CONSTRAINTS — conduit 硬约束

这些是不变量。实现可以换，下面这些必须成立。

## 范围
- 代理集 = `macmini` + `rig` + `MBA`，**每台只管自己本地出站**，不互相当网关。
- `macmini` / `rig` 受管：配置由 conduit 推送 + 远程 reload，mihomo 在线属 fleet 保证态。
- `MBA` 非受管：自取同一份配置工件，conduit / fleet 不保证其状态。
- `aliyun` **不在代理集**（仅 DERP / 证书等基础设施）。

## 两平面分工
- **控制面**（conduit 生成器，慢循环，分钟/小时级）管「成员资格」：哪些节点存在、贴什么标签、长期坏的剔除。
- **数据面**（每台本地 mihomo，快循环，秒级）管「实时存活」：health-check + fallback。
- 推论：节点由生成器 **inline** 进配置，不用 mihomo 原生 proxy-providers 自动抓订阅；新鲜度靠生成器定时跑。

## 标签隔离
- 一个标签 = 一个 proxy-group。**规则只引用 group 名**，永不引用订阅名或具体节点。
- 节点身份用指纹 `(type, server, port)`；订阅改名 / 换订阅后，人工标签仍跟随节点。
- 标签维度正交：`region` / `rate` 自动（正则），`trust` / `purpose` 等人工。
- 没见过指纹的新节点先进**隔离区**（低信任 group），人工打标后转正；不阻塞自动化。

## rule#0：放行 fleet 内网
- Tailscale / DERP 流量必须 **DIRECT 且最高优先级**。具体值（CIDR / 域名 / IP）以 **fleet `STATE.md` 为唯一来源**，conduit 不维护第二份副本（如何单源消费见 ARCHITECTURE 待定项）。
- TUN **不得捕获** tailscale 接口；fake-ip **必须放行** MagicDNS 名称。
- 违反此条会切断 fleet 自身 mesh。

## 可用性目标
- 任一节点故障，对人「无感」（**不是字面 < 1s**）：靠调低 health-check interval + fallback，让新连接秒级绕开死节点。已建立的连接由应用层重试，不强求无缝。
- 长期不健康的节点从生成配置里**剔除**（依据见 ARCHITECTURE 健康回路）。

## 工程约束
- 生成的 mihomo 配置是编译产物，**不手改**。
- 订阅 URL / secret / 生成产物**不进 git**。
- 规则、标签映射、模板长期**版本控制**。
