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
打开 http://127.0.0.1:8000 ：建订阅 → 贴 clash 内容导入 → 看节点池。

## 现在有什么（skeleton）
- `POST /api/subscriptions`、`GET /api/subscriptions`
- `POST /api/subscriptions/{id}/import`（文件导入：normalize → 按 access_id upsert 节点）
- `GET /api/nodes`（不含凭据）
- `GET /`（简单页面）

存储：`service/db.py`（SQLite，三表 subscriptions/imports/nodes）。⚠️ 含明文凭据 = secret 载体，别对公网暴露、别进 git。

⚠️ **骨架暂无认证** —— 只在 uvicorn 默认 `127.0.0.1` 下可接受。上 tailnet 前必须有 Tailscale ACL / 防火墙边界，**别裸绑到共享网络**（认证本身归 later）。

## TODO（后续增量）
tag（地区/人工 + 隔离区）、render + pull（各机拉配置 + reload）、health、traffic、认证、secret 加密、连接并发。
