# conduit-service（骨架）

有状态的控制面：DB + 管理 API + 简单页面。跑在 **rig**（macmini 保持瘦）。core（`conduit/` 纯函数）是引擎，service 套在外面加状态。见仓库根 [ARCHITECTURE.md](../ARCHITECTURE.md)「控制面形态」。

## 跑
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

## TODO（后续增量）
tag（地区/人工 + 隔离区）、render + pull（各机拉配置 + reload）、health、traffic、认证、secret 加密、连接并发。
