# conduit-service（骨架）

有状态的控制面：DB + 管理 API + 简单页面。跑在 **rig**（macmini 保持瘦）。core（`conduit/` 纯函数）是引擎，service 套在外面加状态。见仓库根 [ARCHITECTURE.md](../ARCHITECTURE.md)「控制面形态」。

## 跑

**Docker（rig 上的目标部署，拉公开镜像）**：rig 上只要一份 `deploy/compose.yaml`（不需源码）：
```
CONDUIT_BIND=<rig 私有网 IP> docker compose -f deploy/compose.yaml pull
CONDUIT_BIND=<rig 私有网 IP> docker compose -f deploy/compose.yaml up -d
```
镜像由 GitHub Actions 在 push main / 打 `v*` tag 时自动 build 推到 `ghcr.io/damaged-soda/conduit`（公开包，零认证拉）。DB 落命名卷 `conduit-data`（含凭据，留 rig 磁盘）。默认只绑宿主 `127.0.0.1:8000`；上 tailnet 用 `CONDUIT_BIND` 绑私有网 IP（之后 `rig:8000` 可达）或 `tailscale serve`。**别绑 0.0.0.0**（暂无认证）。

**本地开发（现构建）**：`docker compose -f deploy/compose.yaml up -d --build`。

**本地裸跑（开发）**：
```
pip install -e '.[service]'
uvicorn --factory service.app:make_app   # DB 路径用 CONDUIT_DB，默认 conduit.db
```
打开 http://127.0.0.1:8000 ：建订阅（选择链接 / 文件 / 文本来源）→ 导入/刷新 → 看节点池 → 给节点打地区标签 → 编辑分流策略 → 复制订阅链接导进 clash-verge/mihomo。

## 现在有什么
**订阅 / 节点**
- `GET /api/meta`（版本 / 最近部署时间）
- `POST /api/subscriptions`、`GET /api/subscriptions`（列表不回显 URL，只给 `source_type`/`has_url`）、`GET/PATCH /api/subscriptions/{id}`（管理页编辑用，回显 URL）、`POST /{id}/import`（手动来源导入）、`POST /{id}/refresh`（URL 来源拉取）
- 来源模型：`subscriptions.source_type` 为 `file|url`，当前来源二选一；`url` 来源必须有 URL 且只能刷新，`file` 来源无 URL 且只能手动导入。页面里的文件 / 文本只是手动导入的两种输入方式。`imports` 只记录每次 raw 快照及其来源类型，不代表第二个活动来源。
- 导入格式：Clash/Mihomo YAML、URI 行订阅（ss/vmess/trojan/vless/hysteria/hysteria2）、整份 base64 包裹的 URI/YAML。
- `GET /api/nodes`（不含凭据）

**标签 / 分组**（节点 → 地区组）
- 每节点存 `region`（auto `region_of` + 人工覆盖）+ `quarantined`（隔离），按 access_id 跟着节点走
- `GET /api/groups`（可用目标组：DIRECT/REJECT/PROXY/AUTO + 各地区）

**分流策略**（规则面，category→provider→group；DB 为准，无则回落仓库 `DEFAULT_POLICY`）
- `GET/PUT/DELETE /api/policy`、`GET /api/categories`（geosite/geoip 白名单）、`GET /api/ruleset?kind=&name=`（看类别里匹配啥）
- 页面规则区可只读/编辑（改名/目标下拉/增删匹配/排序/改兜底/存/恢复默认）

**订阅产物**（给 clash-verge/mihomo 导入）
- `GET /sub/clash?token=&full=`：`pure` = proxies + 地区分组 + 规则；`full=1` 再加 fake-ip dns + tun（IPv6 接管 + default-nameserver，见根 [CONSTRAINTS.md](../CONSTRAINTS.md) 「full 模式必须项」）
- `GET /api/sub-token`（+ 页面显示可复制 URL）；token 保护节点凭据，DB `--no-access-log`

**部署侧 mesh DNS 输入**（非 secret，不进 DB）：如调用方有私有 mesh / MagicDNS，可设
`CONDUIT_MESH_DOMAIN_SUFFIXES=ts.net`；full 模式需要专用解析器时再设
`CONDUIT_MESH_DNS_SERVER=100.100.100.100`。这些值会运行时合入 policy：生成 DIRECT 规则、
fake-ip 放行和 `nameserver-policy`，包括已有自定义 policy 的场景。conduit 不内置具体 tailnet 名。

存储：`service/db.py`（SQLite）：`subscriptions(source_type=file|url)/imports/nodes` + `meta`（key=`policy` 存自定义策略 JSON）+ 节点标签。⚠️ 含明文凭据 = secret 载体，别对公网暴露、别进 git。

⚠️ **暂无认证** —— 只在 `127.0.0.1` / tailnet（Tailscale ACL）下可接受，**别裸绑 0.0.0.0**（认证归 later）。

## TODO（后续增量）
health（健康环 + 剔除）、traffic 监控 + 规则建议、订阅定时刷新、认证、secret 加密。
