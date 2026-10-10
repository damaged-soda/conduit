# CONSTRAINTS — conduit 硬约束

这些是不变量。实现可以换，下面这些必须成立。

## conduit 不感知拓扑
- conduit 只做「订阅 → 可靠 mihomo 配置」的生成，**不假定任何具体部署现状**：
  - 目标主机叫什么、有几台、谁推谁拉 —— 是**输入**（调用方提供的 target 清单 + per-target overlay）。
  - 哪些目的地必须直连（私有网 / 内网 / 任何东西）—— 是**输入**（调用方提供的 direct 列表）。
  - 用什么网络把配置送达主机 —— **不是 conduit 的职责**，由调用方决定。
- conduit 仓里只放通用 schema 与占位示例；具体值由调用方从它自己的现状里喂进来。

## 两平面分工
- **控制面**（conduit 生成器）管节点摄入、标签和成员资格；不依据健康结果自动删除或替换正在使用的节点。
- **数据面**（每台客户端）只允许 `select` 手动选择。禁止生成 `fallback`、`url-test`、`load-balance` 或自动测速选优入口；故障、延迟变化、主动测速均不得触发换节点。
- 节点凭据与手动选择由客户端保存；首次导入、所选节点被人工删除或改名后，用户须重新确认选择。已有客户端必须刷新订阅并清理本地自动分组覆盖项后才生效。
- conduit 负责抓取和清洗输入；不让 mihomo 直接抓不可信的原始订阅。

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
full（带 dns+tun）的配置除上面三处外，还有三条不变量，缺一即出事（实战踩过）：
- **TUN 必须同时接管 IPv6**：`ipv6: true` + `dns.ipv6: true` + `tun.inet6-address`（auto-route 才会把 `::/0` 也指向 TUN）。否则系统 IPv6 默认路由仍在物理网卡，浏览器走 IPv6/HTTP3 会**绕过代理直连** → 出口变成本机真实地区 → 按区域封的站（如 claude.ai 看到 `loc=CN`）直接不可用。`route-exclude` 须含 IPv6 本地段（`::1`/`fc00::/7`（含 overlay ULA）/`fe80::/10`），保私有网/SSH 不断。
- **宿主启动不能静默取消 IPv6 接管**：mihomo 启动时若尚未检测到系统 IPv6，会忽略配置里的 TUN IPv6 地址；之后网卡取得公网 IPv6 也不能据此认定 TUN 已补齐。要求双栈接管的宿主须在 mihomo 服务环境设置 `SKIP_SYSTEM_IPV6_CHECK=1`，重启后验证 controller 的 `tun.inet6-address` 和实际 IPv6 路由；仅检查 YAML 或 `mihomo -t` 不够。该变量属于宿主服务配置，不是订阅 YAML。见 [宿主启动与验证](README.md#宿主启动与验证)。
- **TUN 里的真实 IP 连接必须能还原 DIRECT 域名上下文**：full 模式遇到 DIRECT 域名时必须开启 `sniffer`（至少 TLS/443 + QUIC/443 + HTTP/80，`parse-pure-ip: true`，`override-destination: true`），并把这些域名放进 `force-domain` 记录意图。否则 fake-ip 放行后的真实 AAAA 连接进入 TUN 时只剩 IP，`DOMAIN-SUFFIX,...,DIRECT` 可能无法命中，控制面/mesh 域名会被透明代理路径误伤。
- **DNS 必须有 `default-nameserver`（引导）**：含 `system` 任何环境可引导。否则 mihomo 没法做最初解析（连 DoH 服务器都解析不了）→ DNS 引导死锁 → 出网全断。
- **订阅自带节点 DNS 必须按来源域名隔离**：仅允许摄入 `dns.proxy-server-nameserver`，不得继承订阅的
  普通 `nameserver` / 监听 / fake-ip 等全局设置；为该来源实际输出的节点域名编译
  `proxy-server-nameserver-policy`。已有 `nameserver-policy` 同步给其他节点域名；未匹配节点走普通
  `nameserver`，不把 `fallback` 并发混入伪装成有序兜底。同一域名的不同专用 DNS 声明必须让整份
  full 输出拒绝生成，不能并发混用后赌返回顺序。

## 手动选择不变量
- 节点不可用时连接失败，**不能自动切换到另一个代理或直连**。私网及策略中明确声明的 DIRECT 规则继续保持直连。
- `PROXY` 和每个地区组都是 `select`，不生成 `AUTO` / `AUTO-FAST`。旧策略中的自动组目标在渲染时回落到手动 `PROXY`。
- 地区组成员顺序必须可复现：订阅优先级 → 最近一次完整快照的上游原序；地区入口仍按固定地区序展示。顺序不代表故障转移授权。
- Mihomo 输出 `profile.store-selected: true`；客户端有独立的选择缓存时仍由客户端保管。空节点池输出只含 `REJECT` 的手动代理组，保留既有直连分流，不退化为全直连。
- 健康检查仅供用户诊断，不参与自动选择或自动剔除；长连接不会迁移，手动切换只影响新连接。

## 工程约束
- 生成的 mihomo 配置是编译产物，**不手改**。
- 订阅 URL / secret / 调用方喂入的现状值 / 生成产物 **不进 git**。
- 规则、标签映射、模板长期**版本控制**。
