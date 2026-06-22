# ARCHITECTURE — conduit 架构

约束见 [CONSTRAINTS.md](CONSTRAINTS.md)。这里写实现路径，会随迭代变。

## 边界：conduit 的输入
conduit 通过**读外部文件**接收调用方的现状（不硬编码、不反向依赖来源）。约定输入：

- **subscriptions**：结构化来源清单，每条 `id / url|path / type(parser hint) / headers_ref / fetch_interval / source_trust(可选)`（敏感值用 `*_ref` 指外部 secret，放 `secrets/`）；schema 见 `examples/subscriptions.example.yaml`。
- **targets 文件**：目标主机清单 + 每台的渲染相关 overlay（TUN + `route_exclude` / 监听 / DNS+fake-ip / controller bind + `controller_secret_ref`…）。用占位名，conduit 不认识具体主机；schema 见 `examples/targets.example.yaml`。送达 / 谁推谁拉不写这里，那不是 conduit 的事。
- **direct-list 文件**：结构化的必须直连目的地（`domain_exact/suffix/wildcard` + `ip_cidr`）；schema 见 `examples/direct-list.example.yaml`。
- **rules / tags / policies**：自维护的规则、标签映射、策略绑定（版本控制，放 conduit `config/`）。

真值由调用方（如 fleet）按 schema 填，住在调用方那边或某个约定路径；conduit 只认 schema，不认来源。

## 控制面形态（已定，2026-06-20）

- **分层**：`conduit-core`（纯函数库，拓扑/存储无关，golden 守着）↑被驱动↑ `conduit-service`（有状态：DB + 管理 API + 简单页面 + 定时器 + 监控栈）。核心逻辑不随存储/服务变。
- **服务跑 rig**：有状态控制面都在 rig（资源头、把工作面留干净）。**macmini 保持瘦**——人/agent 中枢 + 跑 mihomo（数据面消费者），不背控制面状态（自举困难，故 macmini 少状态）。
- **摄入 = 文件导入**：订阅内容由人在别处下好、导进服务（绕开自举 + 缩小 secret 面：服务不存订阅 URL，只存导入内容里的节点凭据）。网络 `fetch` 推后/可选。
- **下发 = 统一 pull**（不做 push）：每台（macmini/rig/MBA）从 rig 的 API 拉自己的配置 + 本地 reload。受管 = 保证拉到最新；非受管 = 尽力拉。
- **存储可换**：先文件后端，后 DB（SQLite 起步，监控量大再 Postgres/Timescale）。⚠️ DB 含节点凭据 = secret 载体，需访问控制、别对公网暴露。
- **节点身份 v1**：`EndpointId=(type, 规范化 server, port)`；`AccessId=sha256(连接参数去掉显示名)`，人工标签挂 AccessId。

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

## 规则结构（已落地 `conduit/policy.py`）
- 模型 =「规则 = 一组匹配 → 一个目标组」(行业主流 category→provider→group)。`DEFAULT_POLICY` = `routes`(每条 `{name, to:目标组, geosite/geoip/rule_set/domain_suffix/domain/ip_cidr/process_name/dst_port}`，顺序即优先级) + `final`(兜底组)。规则只引用**组名**，永不引用订阅/节点。
- **目标在 render 期校验存在性**：`to`/`final` 不在 {DIRECT,REJECT,PROXY,AUTO,各地区组} 就落到 `final`/PROXY，保证 mihomo 配置合法。
- 匹配来源：内置 `geosite`/`geoip`(mihomo 自带 geo 库，cn/广告大类) + `rule_set`(MetaCubeX `.mrs` 外部规则集，引用而非拷贝、自动更新)。**`.mrs` 只支持 `domain`/`ipcidr`，不支持 `classical`** → process/port 等用显式 `process_name`/`dst_port` 渲成 PROCESS-NAME/DST-PORT。
- **可编辑**：策略存 DB(`meta` key=`policy`)，无则回落仓库 `DEFAULT_POLICY`；页面 / `PUT /api/policy` 改。`rule_providers` 服务端控制(防 PUT 注入 URL → SSRF)、geosite/geoip 走服务端白名单。
- **部署侧 mesh DNS 输入**：调用方可通过 `CONDUIT_MESH_DOMAIN_SUFFIXES` 注入私有 mesh / MagicDNS 后缀；需要专用解析器时用 `CONDUIT_MESH_DNS_SERVER` 生成 `nameserver-policy`。这些运行时合入 policy，不写 DB，不把具体 tailnet 名固化进 conduit。

## 分组 + 订阅输出（已落地）
- **地区分组**(`conduit/tags.py`)：`region_of` **文本关键词优先、旗帜 emoji 兜底**(机场常把台湾标 🇨🇳)；render 按 region 分组 = `PROXY`(select:[AUTO,各地区]) + `AUTO`(fallback) + 每地区一个 fallback 组。标签按 access_id 存 DB、跟节点走。
- **服务以订阅形态下发**：`conduit-service` 把节点池+分组+规则渲成 clash 订阅 `GET /sub/clash?token=&full=`；`pure` 纯净、`full` 加 fake-ip dns + tun（full 模式必须项见 [CONSTRAINTS.md](CONSTRAINTS.md)）。clash-verge/mihomo 直接订阅，等价 `fetch→tag→render` 流水线的产物。

## 目录
```text
conduit/      生成器（Python 包，先放接口骨架）
config/       规则源、标签映射、策略绑定（版本控制）
templates/    mihomo 配置模板 + per-target overlay 钩子
examples/     输入文件的 schema 示例（targets / direct-list，占位值）
secrets/      订阅 URL 等（gitignored）
tests/        测试：golden 配置不变量 + Docker 集成台（见 TESTING.md）
```

## 待定设计点
- 节点身份的人工 `alias/merge/split` 表格式（EndpointId+AccessId 已落地，这是边界细化）。
- 「长期不健康」的具体**阈值与时间窗**、整组全挂的兜底（健康环尚未接）。
- 监控采集器的数据模型与「未识别流量 → 规则建议」的落库形态（独立 collector + 出报告/PR，不自动改规则）。
- 订阅**定时刷新**（节点新鲜度：现导入即静态快照，机场轮换 IP 后会旧）。
- 偏企业级、先标 later 的：controller TLS、订阅 `source_trust` 分级、认证、secret 加密。
