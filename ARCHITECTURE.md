# ARCHITECTURE — conduit 架构

约束见 [CONSTRAINTS.md](CONSTRAINTS.md)。这里写实现路径，会随迭代变。

## 生成流水线（控制面，跑在 macmini）

```text
订阅 → 规范化 → 标签 → 剔除 → 渲染 → 校验 → 下发
fetch  normalize  tag   prune  render validate distribute
```

1. **fetch**：抓订阅原始内容（多格式：clash yaml / base64 等）。来源是 secret，不进 git。
2. **normalize**：解析为统一 `Node` 列表，算指纹 `(type, server, port)`。
3. **tag**：套自动标签（正则）+ 人工标签映射；未见过的指纹进隔离区。
4. **prune**：读健康历史，长期不健康的剔除。
5. **render**：按模板渲染 mihomo 配置（inline proxies + 按标签的 proxy-group + 规则）。
6. **validate**：mihomo 配置自检 + schema 校验；失败即阻断下发。
7. **distribute**：推 macmini / rig + reload；MBA 自取。

## 下发与 reload
- `macmini` / `rig`：经 tailnet 推配置工件 + 打 mihomo reload API（不重启进程，近乎零中断）。
- `MBA`：pull 同一工件。
- 三台配置 95% 相同，差异（TUN / 监听 / 接口 / controller bind）走 per-host overlay（`hosts/`）。

## 健康回路（关联可用性目标与监控目标）

```text
mihomo health-check → Prometheus → 生成器读「过去 N 时长不健康比例」→ prune 剔除 → 重新生成
```

监控栈顺手就喂了剔除决策，不另搭一套健康统计。

## 监控（跑在 macmini）
- 采集器经 tailnet 抓三台 external-controller 的 `/proxies` `/connections` `/traffic` → Prometheus + Grafana（吞吐 / 健康 / 每节点每地区）。
- 连接级原始事件留日志；命中兜底 `MATCH` / `DIRECT` 的 = 未识别流量 → 产出规则建议。
- 全量可见性需要：TUN + sniffer（嗅 SNI）+ 大概率 fake-ip（注意 rule#0 的 fake-ip 放行）。

## 规则结构
- 三层间接：规则 → 策略(意图) → group(物理选择)。规则文件只写 `域名 → 策略:x`，模板绑 `策略:x → group`。换节点选择时不动规则文件。
- 规则用 rule-provider 文件，编译 `.mrs` 提速。

## 目录
```text
conduit/      生成器（Python 包，先放接口骨架）
config/       规则源、标签映射、策略绑定（版本控制）
templates/    mihomo 配置模板 + per-host overlay 钩子
hosts/        per-host overlay（macmini / rig / mba）
secrets/      订阅 URL / controller secret（gitignored）
```

## 待定设计点
- rule#0 放行值如何从 fleet **单源消费**（直接读 fleet 仓 / 共享文件 / 生成时注入），避免两份副本漂移。
- 「长期不健康」的具体**阈值与时间窗**。
- 采集器自写 vs 现成 exporter；起步可先挂 metacubexd 看实时，Prometheus 管道并行搭。
- 指纹是否够稳（CDN 域名 / SNI 落地、同节点多端口等边界）。
