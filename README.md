# conduit

conduit 是 fleet 的出站代理控制面：把一堆不可靠的 VPN 订阅，收敛成一份可靠、可版本控制的 mihomo 配置，下发到受管节点。

这里优先写「应该是什么」，少写「怎么做」。实现路径会变，约束要尽量稳定、好审、可重建。

- 硬约束 / 不变量 → [CONSTRAINTS.md](CONSTRAINTS.md)
- 架构与生成流水线 → [ARCHITECTURE.md](ARCHITECTURE.md)
- 仓库工作约定 → [AGENTS.md](AGENTS.md)

基础设施拓扑（节点、tailnet、DERP、放行值）由 [fleet](https://github.com/damaged-soda/fleet) 负责；conduit 只消费它暴露的事实，不重复维护。

> 订阅 URL、controller secret、生成产物等当前值/凭据不进 git。
