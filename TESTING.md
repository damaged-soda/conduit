# TESTING — conduit 怎么安全地测

约束见 [CONSTRAINTS.md](CONSTRAINTS.md)。这里写怎么验证生成的配置「能用、且不搞断调用方的私有网」，以及**在哪测才安全**。

## 核心原则：宿主机神圣，只在一次性隔离环境里测

危险不是「配置错」，是**失去访问权**。系统级 mihomo 配置（TUN / DNS / 路由）一旦把宿主机网络搞断，你依赖它干活的一切——GitHub、Agent、连通性——全停摆。

所以边界**不是**「本地 vs 远程」，而是：**你正用来干活的宿主机（不管现在哪台、以后迁到哪台）vs 真正一次性的隔离环境**。

- 规则：**永远不在你正用来干活的宿主机上跑实验性 mihomo 系统级配置**。能测的全进 Docker。
- 这条**不随开发机迁移而变**（正因为会迁，原则用「宿主机 vs 隔离环境」写，不绑死哪台）。
- 连带：测试容器留在自己的网络命名空间（**绝不 `--network host`**），即便以后开发机也跑生产 mihomo，容器实验也碰不到宿主网络。

| 环境 | 能否当测试靶 |
|---|---|
| 你正用来干活的宿主机（dev 或 prod） | ❌ 绝不跑实验性系统级配置 |
| Docker 容器（含 privileged TUN，自有 netns） | ✅ 唯一测试环境 |
| 一次性 macOS VM（快照回滚） | 可选，仅当要补 macOS 残差时 |

## Docker 能测什么 / 测不到什么

两件**正交**的事别混：**抓流量方式**（代理 vs TUN）×**跑在哪**（容器 vs 宿主机）。`Docker + TUN` 完全可行 —— privileged 容器（`NET_ADMIN` + `/dev/net/tun`）里 mihomo 建虚拟网卡、`auto-route`、`dns-hijack` 全在容器 netns 内，坏了只坏容器。

**能测（隔离 netns 里，安全）：**
- 全部代理 / 分流 / 故障切换 / 健康检查逻辑（OS 无关）。
- TUN 透明捕获、`auto-route`、直连排除、`dns-hijack`、fake-ip 端到端（Linux TUN 路径）。
- **私有网旁路（最关键，= rule#0「别断 mesh」）** —— 见下。

**测不到（残差）：**
- macOS `utun` 相对 Linux `tun` 的平台差异：路由表是 BSD 的、`dns-hijack` 跟 macOS resolver 的交互、接口探测；overlay（如 tailscale）在 macOS 上无 fwmark、走别的机制。
- 真实线上私有网 / 中继的具体条件。
- → 这些压到**首次谨慎上线**时用 dead-man 自动回退 + break-glass 兜（见末）。

## 关键测试：私有网旁路（rule#0）

调用方的「必须直连」里通常有一张私有网 overlay（在 fleet 场景里是 tailscale 加它的中继 / 控制面）。TUN 一旦把它的**底层**抓走，整张 mesh 就断 —— 这是最危险、最该测的一条，而它**能在 Docker 里 faithful 地测**。

**怎么测：** privileged 容器里跑**真的 overlay**（如 tailscale，join 一个**一次性** tailnet：headscale 或 ephemeral+tagged 节点，测完自动清）+ 同 netns 的 mihomo TUN（带调用方的 direct-list）。断言：**mihomo TUN 开着时，仍能连到 overlay 的 peer**。能连 = 旁路对；连不上 = mesh 被抓断。

**为什么必须用真 overlay、假 peer 糊弄不了：**
- 风险不在逻辑层（app → 私有 IP 走 overlay 接口，排除其 CIDR 即可），而在**底层**：overlay 守护进程把流量加密后，以 UDP 发往**中继 / peer 的真实公网 IP**，走默认路由 → 正好被 mihomo TUN 抓走，隧道就废了。
- 推论：**direct-list 不只是排除私有网 CIDR，还要排除中继 IP / 控制面域名 / 处理底层**。这只有跑真 overlay 才暴露得出来。
- 平台差异也在这里放大：Linux 上 overlay 用 fwmark + ip rule 防回环，跟 mihomo 的 auto-route 谁优先只能实测；macOS 机制不同（属上面的残差）。

## 分层（便宜 → 贵）

1. **golden 配置不变量**（零网络）—— `tests/`：断言 direct-list 三处覆盖一致、规则只引用 group 不指向节点、DIRECT 必须最前等。最安全，先跑。
2. **Docker 集成**（隔离 Linux netns）—— `tests/integration/`：代理 + TUN + **私有网旁路**，实测路由 / 故障切换 / mesh 不断。
3. **首次上线 + 安全网**：只放过了 1、2 的配置。`dead-man` timer（不续命就自动回退到已知 good / 纯直连）、reload 不 restart、apply 前先 validate。**break-glass 入口也必须在 direct-list 里**，且 rollback 机制**不依赖 mihomo 本身**。
   - macOS 残差默认压到这一层用安全网兜；真不放心，再加一次性 macOS VM 单独冒烟。

## Docker 注意点
- 代理端口测路由 / failover：最简单。
- TUN：容器 `cap_add: [NET_ADMIN]` + `/dev/net/tun`，留自己的 netns，绝不 `--network host`。
- 私有网旁路：privileged 容器 + 真 overlay + 一次性控制面（如 headscale），别拿真私有网冒险 / 污染。

## 已知待补（按 Codex review）
- **私有网旁路变体**还没落成 compose（headscale + tailscale + privileged mihomo-TUN + peer 可达性断言）——设计见上「关键测试」。
- **规则解析**：已做括号感知切分；SUB-RULE 递归校验仍 TODO。
- **fake-ip**：`fake-ip-filter-mode: rule`、私有域名的 `nameserver-policy` / `direct-nameserver` 需求未纳入断言。
- **direct-list**：`domain_wildcard` → mihomo 规则类型映射待 render 设计（mihomo 无 `DOMAIN-WILDCARD` 规则）。
- **可用性**：fallback 健康检查频率预算；group health-check 不覆盖 `use:` provider —— 数据面 fallback 要么不用 `use:`、要么 provider 另配 health-check。
- **集成断言**：量化切换耗时、用 `/proxies` 确认选中节点、长连接确认 chain；镜像固定版本。
- **break-glass**：拆成 域名 / 固定 IP / 解析 DNS / 控制面入口，并在残差冒烟里实测 rollback timer 不依赖 mihomo。
- **生产不变量**：controller 绑定（生产须 loopback / 受控）+ secret 必须存在。
- `mihomo -t -f` 已接入 golden（装了才跑）；后续补坏配置负例语料。
