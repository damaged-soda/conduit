# ARCHITECTURE — conduit 架构

约束见 [CONSTRAINTS.md](CONSTRAINTS.md)。这里写实现路径，会随迭代变。

## 边界：conduit 的输入
conduit 通过**读外部文件**接收调用方的现状（不硬编码、不反向依赖来源）。约定输入：

- **subscriptions**：结构化来源清单，每条 `id / url|path / type(parser hint) / headers_ref / fetch_interval / source_trust(可选)`（敏感值用 `*_ref` 指外部 secret，放 `secrets/`）；schema 见 `examples/subscriptions.example.yaml`。
- **targets 文件**：目标主机清单 + 每台的渲染相关 overlay（TUN + `route_exclude` / 监听 / DNS+fake-ip / controller bind + `controller_secret_ref`…）。用占位名，conduit 不认识具体主机；schema 见 `examples/targets.example.yaml`。送达 / 谁推谁拉不写这里，那不是 conduit 的事。
- **direct-list 文件**：结构化的必须直连目的地（`domain_exact/suffix/wildcard` + `ip_cidr`）；schema 见 `examples/direct-list.example.yaml`。
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

## 输出形态（影响健康检查，要先定）
节点写进配置有两种形态，二选一 —— 它决定故障切换 / 剔除能不能生效：
- **top-level `proxies` + group 直接列成员**：group 的 health-check 覆盖每个节点。简单直接，推荐起步。
- **conduit 生成 `file`/`inline` proxy-provider + group `use:`**：更贴 mihomo 习惯，但 group health-check **不覆盖 `use:` 来的节点**，必须改用 **provider 级 health-check**，否则切换 / 剔除失效。
两种都一样：原始不可信订阅由 conduit 抓 + 清洗，绝不让 mihomo 直接拉。

## 健康回路（关联可用性目标与监控目标）

```text
mihomo health-check → 指标存储 → 生成器读「过去 N 时长不健康比例」→ prune 剔除 → 重新生成
```

- conduit 只消费一个标准的「节点健康历史」接口，schema 大致：`access_id / rendered_proxy_name / target / group / ts / status / latency / source`。关键是把 mihomo runtime 里的 proxy 名稳定映射回 `access_id`。
- 指标怎么采、存哪（Prometheus / 别的）是**部署细节，调用方定**。

## 规则结构
- 三层间接：规则 → 策略(意图) → group(物理选择)。规则文件只写 `域名 → 策略:x`，模板绑 `策略:x → group`。换节点选择时不动规则文件。
- **policy 在编译期绑成具体 group 名**（mihomo 规则只能指向具体 proxy/group；`RULE-SET,name,target` 才带目标）。
- 大的 domain/ipcidr 集合编 `.mrs` 提速；**`.mrs` 目前只支持 `domain` / `ipcidr`，不支持 `classical`** —— process / port / 逻辑 / classical 规则保留 YAML/text。

## 目录
```text
conduit/      生成器（Python 包，先放接口骨架）
config/       规则源、标签映射、策略绑定（版本控制）
templates/    mihomo 配置模板 + per-target overlay 钩子
examples/     输入文件的 schema 示例（targets / direct-list，占位值）
secrets/      订阅 URL 等（gitignored）
tests/        测试：golden 配置不变量 + Docker 集成台（见 TESTING.md）
```

## 待定设计点（按 Codex review 收敛后）
- **节点身份模型**（下一轮先啃）：`access_id` 精确进哪些参数、proxy 命名规则、人工 alias/merge/split 表的格式。
- 「长期不健康」的具体**阈值与时间窗**、整组全挂的兜底。
- 监控采集器的数据模型与「未识别流量 → 规则建议」的落库形态（独立 collector + 出报告/PR，不自动改规则）。
- 偏企业级、先标 later 的：controller TLS、订阅 `source_trust` 分级。
