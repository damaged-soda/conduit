# conduit

conduit 把一堆不可靠的订阅，收敛成一份可靠、可版本控制的 mihomo 配置。

它只做「生成」：输入订阅、规则、标签，以及一组由**调用方**提供的部署事实（目标主机、必须直连的目的地等），输出每台目标的 mihomo 配置。

conduit **不感知任何具体拓扑** —— 主机叫什么、有没有私有网、哪些地址要直连，全是输入，不在 conduit 里硬编码。这样规则系统既独立于易变的订阅，也独立于易变的部署现状。

- 硬约束 / 不变量 → [CONSTRAINTS.md](CONSTRAINTS.md)
- 架构与生成流水线 → [ARCHITECTURE.md](ARCHITECTURE.md)
- 仓库工作约定 → [AGENTS.md](AGENTS.md)

> 订阅 URL、secret、调用方喂入的现状值、生成产物 —— 都不进 git。
