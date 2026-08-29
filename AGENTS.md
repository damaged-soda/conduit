# AGENTS.md — conduit 工作约定

本仓属于 personal 域；这里只保留 conduit 自身
当前真用得上的约束。

## 改动走 PR
- 任何改动开新分支（ASCII kebab，如 `feat/...` / `chore/...`）→ push → `gh pr create` 提 review + merge。
- 不直接提交到 `main`（首次 bootstrap 骨架除外）。
- Codex 常在临时 worktree / detached HEAD 里干活；如果 `main` 已被 `/Users/leavan/work/conduit` 这类主 worktree 占用，不要强切，合并后在主 worktree `git pull --ff-only`，当前 worktree detach 到 `origin/main`；本地/远端分支的清理按下一条授权规则。
- 合并后如用户明确授权删除，再清理远端/本地 feature 分支；仅确认“已合并”不构成删除分支或
  worktree 的授权。未获授权时保留现场，但仍抓取远端并把本地默认分支安全快进到远端。

## 发布 tag
- 合并到 `main` 后，如本次变更需要触发发布镜像/留 release 点，打 annotated tag：`git tag -a vX.Y.Z -m "conduit vX.Y.Z" <merge-commit>` → `git push origin vX.Y.Z`。
- 现有节奏是 patch 递增（如 `v0.1.5` → `v0.1.6`），tag 打在 PR 的 merge commit 上。

## 生产部署

- tag 的 `build-image` run 成功、`ghcr.io/damaged-soda/conduit:vX.Y.Z` 就绪后，
  将 canonical main 安全快进到 `origin/main`，在 macmini 执行：
  ```sh
  cd /Users/leavan/work/personal/conduit
  bin/conduit-deploy --production
  ```
- `bin/conduit-deploy` 是生产部署单写者：只接受 macmini canonical main 且现场要求
  `HEAD == origin/main`，把仓内 `deploy/compose.yaml` 投影到 rig，先显式 pull，随后
  禁止 build / 二次 pull 地原地收敛现有 `conduit-service` project，并回验 localhost
  与 tailnet `/api/meta` 的版本。默认跟踪发布指针 `release`，不维护中央版本 pin；
  rig 侧用 app-owned 锁拒绝并发部署交错。
- 常规回滚不改仓内默认值，显式执行
  `bin/conduit-deploy --production --image-tag vX.Y.Z`；配置回滚走本仓 revert PR，
  合并后重新部署。

## secrets 永不进 git
- 订阅 URL / API key / controller secret / `.env` 一处存、`.gitignore`、**绝不提交**。
- 一旦误推，按「泄露」处理：立即轮换该凭据。

## 本地 Codex 状态
- `.codex/` 是 Codex Desktop 生成的本机/worktree 状态（例如 `environments/environment.toml`），默认 ignore，不进仓库。
- 需要共享给 agent 的项目约定写进 `AGENTS.md`；不要改/提交 `.codex/` 里的自动生成文件。

## 身份
- commit 用个人号 `leavan <damaged.soda@gmail.com>`。
- `gh` 操作用个人号 `damaged-soda`。

## 配置是编译产物
- 生成出来的 mihomo 配置**不手改**；要改就改源（规则 / 标签 / 模板 / 订阅）再重新生成。
