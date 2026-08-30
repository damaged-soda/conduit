# conduit-service（骨架）

有状态的控制面：DB + 管理 API + 简单页面。跑在 **rig**（macmini 保持瘦）。core（`conduit/` 纯函数）是引擎，service 套在外面加状态。见仓库根 [ARCHITECTURE.md](../ARCHITECTURE.md)「控制面形态」。

## 跑

**Docker（拉公开镜像）**：只要一份 `deploy/compose.yaml`（不需源码）：
```
docker compose -f deploy/compose.yaml pull \
  && docker compose -f deploy/compose.yaml up -d --no-build --pull never
```
（`&&` 短路 + `--no-build --pull never` 缺一不可：Compose 对可构建服务会吞掉拉取
错误回退本机构建，显式 `pull` 才 fail loud、`up` 禁构建禁重拉。）镜像由 GitHub
Actions 在 push main / 打 `v*` tag 时自动 build 推到
`ghcr.io/damaged-soda/conduit`（公开包，零认证拉）。compose 默认跟踪 `release`
（随 `v*` tag 移动的已发布指针）；生产统一从 macmini canonical main 运行
`bin/conduit-deploy --production`，部署特定版本显式加 `--image-tag vX.Y.Z`。DB 落
命名卷 `conduit-data`（含凭据，留 rig 磁盘）。默认只绑宿主 `127.0.0.1:8000`；
tailnet 暴露走宿主 `tailscale serve`（`svc:conduit`）。**别绑 0.0.0.0**（暂无认证）。

**本地开发（现构建）**：`docker compose -f deploy/compose.yaml up -d --build`。

**本地裸跑（开发）**：
```
pip install -e '.[service]'
uvicorn --factory service.app:make_app   # DB 路径用 CONDUIT_DB，默认 conduit.db
```
打开 http://127.0.0.1:8000 ：建订阅（选择链接 / 文件 / 文本来源）→ 拖动订阅设置优先级 →
导入/刷新 → 看节点池 → 给节点打地区标签 → 编辑分流策略 → 复制订阅链接导进
clash-verge/mihomo。

## 现在有什么
**订阅 / 节点**
- `GET /api/meta`（版本 / 最近部署时间）
- `POST /api/subscriptions`、`GET /api/subscriptions`（按优先级从高到低，即 `position` 升序返回；不回显 URL，只给
  `source_type`/`has_url`）、`PUT /api/subscriptions/order`（完整 id 列表原子换序）、
  `GET/PATCH /api/subscriptions/{id}`（管理页编辑用；详情回显 URL 与最近一次成功导入的完整 `raw`，
  二者均为 secret，后者含节点凭据；不出现在列表 / 节点接口）、
  `POST /api/subscriptions/{id}/import`（手动来源导入；URL 来源须显式传 `detach_url=true`）、
  `POST /api/subscriptions/{id}/refresh`（URL 来源拉取）
- 来源模型：`subscriptions.source_type` 为 `file|url`，当前来源二选一；`url` 来源必须有 URL 并通过刷新更新，`file` 来源无 URL 并通过手动导入更新。页面里的文件 / 文本只是手动导入的两种输入方式；订阅详情会把最近一次成功导入的原文带入文本编辑器。对 URL 来源手动导入时，页面会确认解除 URL，API 也要求显式 opt-in；解析和写库成功后才在同一事务内切换为 `file`，失败则保留 URL 与旧快照。`imports` 只记录每次 raw 快照及其来源类型，不代表第二个活动来源。
- 导入格式：Clash/Mihomo YAML、URI 行订阅（ss/vmess/trojan/vless/hysteria/hysteria2）、整份
  base64 包裹的 URI/YAML；每次成功导入是该订阅的完整节点快照，保留上游原序并移除已消失节点；
  导入失败则保留旧快照。订阅渲染只输出支持 UDP 的代理节点。
- `GET /api/nodes`（不含凭据）

**标签 / 分组**（节点 → 地区组）
- 每节点存 `region`（auto `region_of` + 人工覆盖）+ `quarantined`（隔离），按 access_id 跟着节点走
- `GET /api/groups`（可用目标组：DIRECT/REJECT/PROXY/AUTO + 各地区）

**分流策略**（规则面，category→provider→group；DB 为准，无则回落仓库 `DEFAULT_POLICY`）
- `GET/PUT/DELETE /api/policy`、`GET /api/categories`（geosite/geoip 白名单）、`GET /api/ruleset?kind=&name=`（看类别里匹配啥）
- 页面规则区可只读/编辑（改名/目标下拉/增删匹配/排序/改兜底/存/恢复默认）

**订阅产物**（给客户端导入）
- `GET /sub/clash?token=&full=`：`pure` = proxies + 地区分组 + 规则；`full=1` 再加 fake-ip dns + tun（IPv6 接管 + default-nameserver，见根 [CONSTRAINTS.md](../CONSTRAINTS.md) 「full 模式必须项」）
- `GET /sub/stash?token=`：Stash 专用 pure 配置；在相同节点、分组和规则上追加一个不含
  `auth-key`/hostname/tailnet 地址的原生 `type: tailscale` 节点。首次导入后从该节点菜单进入
  Tailscale 页面交互登录；普通 `/sub/clash` 不含 Stash 专用字段。
- Clash 来源若自带 `dns.proxy-server-nameserver`，导入会把它作为 secret 元数据随当前快照保存；
  `full=1` 按该来源实际节点的精确域名生成 `proxy-server-nameserver-policy`，不会接管其他订阅或普通
  目标域名。其他订阅 DNS 字段仍全部丢弃；相同节点域名声明不同专用 DNS 时，整份 `full=1`
  输出返回 409（`pure` 不受影响）。启用来源 policy 后，未匹配节点使用普通 `nameserver`；已有
  `nameserver-policy` 会同步到节点 policy，`fallback` 不作为有序兜底继承。
- 导出节点名为 `[订阅名] 原节点名`。每个地区 `fallback` 的成员顺序为“订阅优先级 →
  上游原序”；默认 `AUTO-FAST` 仍跨全部节点按延迟选优。拖动后服务端产物立即变化，客户端刷新订阅后生效。
- `GET /api/sub-token`（+ 页面显示可复制 URL）；token 保护节点凭据，DB `--no-access-log`

**部署侧 mesh DNS 输入**（非 secret，不进 DB）：`deploy/compose.yaml` 默认注入 Tailscale 通用常量
`CONDUIT_MESH_DOMAIN_SUFFIXES=ts.net` 和 `CONDUIT_MESH_DNS_SERVER=100.100.100.100`，可用同名环境
变量覆盖；裸跑或其他部署方式按实际 mesh 需要设置。这些值会运行时合入 policy：生成 DIRECT 规则、
fake-ip 放行和 `nameserver-policy`，包括已有自定义 policy 的场景。conduit 不内置具体 tailnet 名。

存储：`service/db.py`（SQLite）：`subscriptions(position, source_type=file|url)` / `imports` /
`nodes(sub_id, position)` + `meta`（key=`policy` 存自定义策略 JSON）+ 节点标签。旧库升级时按创建时间
初始化订阅优先级，并尽量从最近一次 `imports.raw` 重建节点原序。⚠️ 含明文凭据 = secret 载体，
别对公网暴露、别进 git。

⚠️ **暂无认证** —— 只在 `127.0.0.1` / tailnet（Tailscale ACL）下可接受，**别裸绑 0.0.0.0**（认证归 later）。

## TODO（后续增量）
health（健康环 + 剔除）、traffic 监控 + 规则建议、订阅定时刷新、认证、secret 加密。
