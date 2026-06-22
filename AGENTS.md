# AGENTS.md — conduit 工作约定

沿用 fleet 的硬规矩，只放当前真用得上的。

## 改动走 PR
- 任何改动开新分支（ASCII kebab，如 `feat/...` / `chore/...`）→ push → `gh pr create` 提 review + merge。
- 不直接提交到 `main`（首次 bootstrap 骨架除外）。
- 合并后回 `main` `pull` + 删本地分支，保持开局干净。

## secrets 永不进 git
- 订阅 URL / API key / controller secret / `.env` 一处存、`.gitignore`、**绝不提交**。
- 一旦误推，按「泄露」处理：立即轮换该凭据。

## 身份
- commit 用个人号 `leavan <damaged.soda@gmail.com>`。
- `gh` 操作用个人号 `damaged-soda`。

## 配置是编译产物
- 生成出来的 mihomo 配置**不手改**；要改就改源（规则 / 标签 / 模板 / 订阅）再重新生成。
