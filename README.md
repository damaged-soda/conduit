# conduit

conduit 把一堆不可靠的订阅，收敛成一份可靠、可版本控制的 mihomo 配置。

它只做「生成」：输入订阅、规则、标签，以及一组由**调用方**提供的部署事实（目标主机、必须直连的目的地等），输出每台目标的 mihomo 配置；有状态服务还可下发 Clash/Mihomo、Stash、Shadowrocket 和 Surge 订阅。

conduit **不感知任何具体拓扑** —— 主机叫什么、有没有私有网、哪些地址要直连，全是输入，不在 conduit 里硬编码。这样规则系统既独立于易变的订阅，也独立于易变的部署现状。

- 硬约束 / 不变量 → [CONSTRAINTS.md](CONSTRAINTS.md)
- 架构与生成流水线 → [ARCHITECTURE.md](ARCHITECTURE.md)
- 有状态管理服务（订阅来源编辑 / API / 部署）→ [service/README.md](service/README.md)
- 仓库工作约定 → [AGENTS.md](AGENTS.md)
- 怎么安全地测 → [TESTING.md](TESTING.md)
- 主机侧拉取 hook（pull + 本地 overlay + 校验 + 原子安装 + 重启）→ `scripts/conduit-mihomo-pull.py`

> 订阅 URL、secret、调用方喂入的现状值、生成产物 —— 都不进 git。

## 宿主启动与验证

`full=1` 已生成双栈 TUN 配置。mihomo 启动时的系统 IPv6 探测可能早于网卡就绪，
从而移除运行态的 TUN IPv6 地址。要求双栈接管的宿主还须在 **mihomo 服务进程**环境
设置 `SKIP_SYSTEM_IPV6_CHECK=1`；终端 export 或订阅 YAML 不能替代服务环境。
这是 [mihomo 官方支持的启动选项](https://wiki.metacubex.one/en/config/inbound/tun/#inet6-address)。

macOS root LaunchDaemon：在已有 plist 的 `EnvironmentVariables` 字典中加入该变量，
保留其他字段、root 所有权和权限。修改前备份服务定义，并设置不依赖代理的自动回退；
修改后用 `launchctl bootout` / `bootstrap` 重新加载定义。日常重启用
`sudo launchctl kickstart -k system/homebrew.mxcl.mihomo`，本仓 pull hook 也使用该入口，
保留已加载的服务环境；服务未加载时报错，不转而重建服务定义。
Homebrew 的 root service 不支持用户 `services/mihomo.env` 覆盖；手动执行
`brew services restart` 或升级后若重建了 plist，必须重新应用并检查该环境变量。
Linux systemd 使用 service drop-in 的 `Environment=SKIP_SYSTEM_IPV6_CHECK=1`，
然后 `daemon-reload` 并重启 mihomo。

验收须同时检查：controller `/configs` 的 `tun.inet6-address` 非空；公网 IPv4/IPv6
目的地址均走 TUN；私有网/SSH 仍可达；强制 IPv6 的请求在 mihomo 连接记录中命中
预期分流。对比同一公开 URL 的默认请求、`curl -4`、`curl -6` 和显式 HTTP 代理请求，
不能把节点名称、Cloudflare PoP 或 IPv4 请求成功当作没有 IPv6 泄漏的证明。
