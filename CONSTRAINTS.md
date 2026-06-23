# CONSTRAINTS — conduit 硬约束

这些是不变量。实现可以换，下面这些必须成立。

## conduit 不感知拓扑
- conduit 只做「订阅 → 可靠 mihomo 配置」的生成，**不假定任何具体部署现状**：
  - 目标主机叫什么、有几台、谁推谁拉 —— 是**输入**（调用方提供的 target 清单 + per-target overlay）。
  - 哪些目的地必须直连（私有网 / 内网 / 任何东西）—— 是**输入**（调用方提供的 direct 列表）。
  - 用什么网络把配置送达主机 —— **不是 conduit 的职责**，由调用方决定。
- conduit 仓里只放通用 schema 与占位示例；具体值由调用方从它自己的现状里喂进来。

## 两平面分工
- **控制面**（conduit 生成器，慢循环，分钟/小时级）管「成员资格」：哪些节点存在、贴什么标签、长期坏的剔除。
- **数据面**（每台本地 mihomo，快循环，秒级）管「实时存活」：health-check + fallback。
- 推论：**conduit 自己负责 fetch / 标签 / 剔除，不让 mihomo 直接抓不可信的原始订阅**。输出形态二选一（见 ARCHITECTURE）：写成 top-level `proxies`，或生成 `file`/`inline` provider；新鲜度靠生成器定时跑。

## 标签隔离
- **proxy-group = 一个标签表达式**（可跨维度组合，如 `trusted ∩ hk ∩ streaming`）；**规则只引用 group 名**，永不引用订阅名或具体节点。render 时把表达式展开成显式成员。
- 节点身份**分两层**：`endpoint_id = (type, 规范化 server, port)` 做粗物理聚合；`access_id = sha256(规范化连接参数去掉显示名)` 做稳定身份，**人工标签挂在 `access_id` 上**，订阅改名 / 换订阅后仍跟随。连接参数包括 sni / network / ws-path / grpc-service / cipher / uuid|password 等；若后续要改 HMAC，必须先有稳定 key 管理，避免打断既有标签。边界（同机多协议、CDN 落地变化等）用人工 alias / merge / split 表处理。（精确参数集与表格式见后续身份模型设计轮。）
- 标签维度正交：`region` / `rate` 自动（正则），`trust` / `purpose` 等人工。
- 没见过指纹的新节点先进**隔离区**（低信任 group），人工打标后转正；不阻塞自动化。

## 直连列表（generic bypass）
- 调用方提供一组「必须直连」的目的地（结构化：`domain_exact` / `domain_suffix` / `domain_wildcard` / `ip_cidr`）。conduit **不关心里面是什么、为什么**。
- 「必须直连」在 mihomo 里要**同时落到三处**，缺一不可：① 最高优先级 DIRECT 规则；② fake-ip 放行（进 `fake-ip-filter` / real-ip）；③ TUN 路由排除（`route-exclude-address`）。私有域名可能还需 `nameserver-policy` 指向直连 DNS。
- validate 阶段必须检查这三处覆盖一致。

## full 模式（TUN）必须项
full（带 dns+tun）的配置除上面三处外，还有两条不变量，缺一即出事（实战踩过）：
- **TUN 必须同时接管 IPv6**：`ipv6: true` + `dns.ipv6: true` + `tun.inet6-address`（auto-route 才会把 `::/0` 也指向 TUN）。否则系统 IPv6 默认路由仍在物理网卡，浏览器走 IPv6/HTTP3 会**绕过代理直连** → 出口变成本机真实地区 → 按区域封的站（如 claude.ai 看到 `loc=CN`）直接不可用。`route-exclude` 须含 IPv6 本地段（`::1`/`fc00::/7`（含 overlay ULA）/`fe80::/10`），保私有网/SSH 不断。
- **DNS 必须有 `default-nameserver`（引导）**：含 `system` 任何环境可引导。否则 mihomo 没法做最初解析（连 DoH 服务器都解析不了）→ DNS 引导死锁 → 出网全断。

## 可用性目标
- SLO 写清楚，别含糊：**新连接在节点被探测判定不健康后数秒内绕开**；**已建立的连接不迁移，由应用层重试**（mihomo 不会把在途连接搬到别的节点）。不承诺字面 < 1s、不承诺无感。
- 落地参数：`fallback` 组 + `health-check` 的 `interval` / `timeout` / `lazy: false` / `expected-status` / `max-failed-times` 调到位；并定义**整组全挂时**的兜底行为。
- ⚠️ mihomo 的 group health-check **只检查直接写在 `proxies:` 的节点，不检查通过 `use: [provider]` 引入的 provider 节点** —— 这条决定上面「输出形态」的选择（见 ARCHITECTURE）。
- 长期不健康的节点从生成配置里**剔除**（依据见 ARCHITECTURE 健康回路）。

## 工程约束
- 生成的 mihomo 配置是编译产物，**不手改**。
- 订阅 URL / secret / 调用方喂入的现状值 / 生成产物 **不进 git**。
- 规则、标签映射、模板长期**版本控制**。
