# TESTING — conduit 怎么安全地测

约束见 [CONSTRAINTS.md](CONSTRAINTS.md)。这里写怎么验证生成的配置「能用且不搞断 fleet」。

## 核心原则：别在「弄坏了就回不去」的地方测

危险的本质不是配置错，而是**失去访问权**。mihomo 的 TUN / DNS / 路由是系统级、全机生效的——一个坏配置能把整机流量黑洞、或把私有网接口也捕获，那台机器就**远程登不回去了**。

| 机器 | 怎么访问 | 测试定位 |
|---|---|---|
| `macmini` / `rig` | 经 tailnet **远程** | 配坏 = 锁死。**绝不做实验**，只接收过了校验的配置 + 安全网 |
| `MBA` | **本地物理** 就在跟前 | 天然 staging：坏了关掉 mihomo / 重启就回来，可恢复 |
| Docker | 网络命名空间**隔离** | 随便坏，宿主机无恙 |

**安全网（给生产机）**：fleet 有走 tailnet 之外的公网 break-glass（见 fleet STATE）。真把 tailnet 搞断了还能从公网捞回来 —— **但前提是这些 break-glass 入口本身在 direct-list 里**，否则也被 TUN 吃掉。⇒ 设计含义：direct-list 不只放私有网，还要放 break-glass 路径。

## 分层策略（便宜→贵、安全→风险）

1. **结构化 / golden 配置测试（零网络，最安全）** —— `tests/test_config_invariants.py`
   断言生成的 YAML：direct-list 是否同时落到 DIRECT 规则 + fake-ip 放行 + TUN route-exclude；规则只引用 group 名、不指向具体节点；group 成员都存在。旁路正确性的大半 bug 在这层就能抓到，完全不碰网络。再配 `mihomo -t -f <config>` 做配置自检。

2. **Docker 集成（Linux 隔离）** —— `tests/integration/`
   compose 起 mihomo + 可 kill 的 mock 上游 + client，经**代理端口 / external-controller API** 验路由与故障切换（先不开 TUN）。kill 一个上游 → 断言新连接改走别的节点、量切换耗时对照 SLO。可复现、可进 CI。
   ⚠️ Docker on Mac 是 Linux VM，这层验的是 **Linux/rig 路径**。

3. **MBA 实机冒烟（macOS 原生）**
   macOS 原生 utun + 真实 fake-ip/DNS 只能在真 Mac 上验。MBA 安全（本地可恢复）：跑过校验的配置 → 确认上网走代理、tailscale 仍可达、kill 节点能切、然后关掉。

4. **上生产（macmini / rig）+ 安全网**
   只放过了 1～3 的配置。外加 **dead-man 开关**（launchd/systemd timer，不续命就自动回退到已知 good / 纯直连）、reload 不 restart、apply 前先 validate。

## Docker 注意点
- 纯代理端口测路由 / failover：简单，先做这个。
- 测 TUN：容器要 `cap_add: [NET_ADMIN]` + `/dev/net/tun`。
- 忠实测私有网旁路：在同 netns 里把那张私有网（如 tailscale）也跑起来，再断言其流量走直连。晚点需要再上。

## 已知待补（按 Codex 第 2 轮 review）
- **规则解析**：当前已做括号感知切分；SUB-RULE 的递归校验仍 TODO。
- **fake-ip**：`fake-ip-filter-mode: rule`、私有域名的 `nameserver-policy` / `direct-nameserver` 需求未纳入断言。
- **direct-list**：`domain_wildcard` → mihomo 规则类型映射待 render 设计（mihomo 无 `DOMAIN-WILDCARD` 规则）。
- **可用性**：fallback 健康检查频率预算（节点多时别打爆带宽）；group health-check 不覆盖 `use:` provider —— 数据面 fallback 要么不用 `use:`、要么 provider 另配 health-check。
- **集成断言**：量化切换耗时、用 `/proxies` 确认选中节点、长连接确认 chain；镜像固定版本。
- **break-glass**：要拆成 域名 / 固定 IP / 解析 DNS / 控制面入口，并在 MBA 冒烟里实测 rollback timer 不依赖 mihomo 本身。
- **生产不变量**：controller 绑定（生产须 loopback / 受控）+ secret 必须存在。
- `mihomo -t -f` 已接入 golden（装了 mihomo 才跑）；后续补坏配置负例语料。
