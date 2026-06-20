# ARCHITECTURE — conduit 架构

约束见 [CONSTRAINTS.md](CONSTRAINTS.md)。这里写实现路径，会随迭代变。

## 边界：conduit 的输入
conduit 通过**读外部文件**接收调用方的现状（不硬编码、不反向依赖来源）。约定输入：

- **subscriptions**：订阅来源（secret，放 `secrets/`）。
- **targets 文件**：目标主机清单 + 每台的渲染相关 overlay（TUN / 监听 / controller bind…）。用占位名，conduit 不认识具体主机；schema 见 `examples/targets.example.yaml`。送达 / 谁推谁拉不写这里，那不是 conduit 的事。
- **direct-list 文件**：必须直连的目的地；schema 见 `examples/direct-list.example.yaml`。
- **rules / tags / policies**：自维护的规则、标签映射、策略绑定（版本控制，放 conduit `config/`）。

真值由调用方（如 fleet）按 schema 填，住在调用方那边或某个约定路径；conduit 只认 schema，不认来源。

## 生成流水线（控制面）

```text
fetch → normalize → tag → prune → render → validate
```

1. **fetch**：抓订阅原始内容（多格式：clash yaml / base64 等）。
2. **normalize**：解析为统一 `Node`，算指纹。**丢弃订阅自带的规则系统**。
3. **tag**：auto（正则）+ manual（映射）；未见过的指纹进隔离区。
4. **prune**：按健康历史剔除长期不健康节点（阈值/时间窗待定）。
5. **render**：按模板渲染**某个 target** 的 mihomo 配置（inline proxies + 标签 group + 规则 + 注入 direct-list + per-target overlay）。
6. **validate**：mihomo 配置自检 + schema 校验；失败即阻断。

**送达不在核心流水线里**：conduit 产出 per-target 工件，怎么送到主机、怎么 reload，由调用方决定（conduit 至多提供通用 hook）。

## 健康回路（关联可用性目标与监控目标）

```text
mihomo health-check → 指标存储 → 生成器读「过去 N 时长不健康比例」→ prune 剔除 → 重新生成
```

指标怎么采、存哪（Prometheus / 别的），是**部署细节，调用方定**；conduit 只消费一个「节点健康历史」接口。

## 规则结构
- 三层间接：规则 → 策略(意图) → group(物理选择)。规则文件只写 `域名 → 策略:x`，模板绑 `策略:x → group`。换节点选择时不动规则文件。
- 规则用 rule-provider 文件，编译 `.mrs` 提速。

## 目录
```text
conduit/      生成器（Python 包，先放接口骨架）
config/       规则源、标签映射、策略绑定（版本控制）
templates/    mihomo 配置模板 + per-target overlay 钩子
examples/     输入文件的 schema 示例（targets / direct-list，占位值）
secrets/      订阅 URL 等（gitignored）
```

## 待定设计点
- 「长期不健康」的具体**阈值与时间窗**。
- 指纹是否够稳（CDN 域名 / SNI 落地、同节点多端口等边界）。
- 健康指标的**采集与存储形态**（自写 vs 现成），属部署侧。
