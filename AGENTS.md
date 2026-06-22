# AGENTS.md — conduit 工作约定

沿用 fleet 的硬规矩，只放当前真用得上的。

## 改动走 PR
- 任何改动开新分支（ASCII kebab，如 `feat/...` / `chore/...`）→ push → `gh pr create` 提 review + merge。
- 不直接提交到 `main`（首次 bootstrap 骨架除外）。
- Codex 常在临时 worktree / detached HEAD 里干活；如果 `main` 已被 `/Users/leavan/work/conduit` 这类主 worktree 占用，不要强切，合并后在主 worktree `git pull --ff-only`，当前 worktree detach 到 `origin/main` 再删本地分支。
- 合并后删远端/本地 feature 分支，保持开局干净。

## 发布 tag
- 合并到 `main` 后，如本次变更需要触发发布镜像/留 release 点，打 annotated tag：`git tag -a vX.Y.Z -m "conduit vX.Y.Z" <merge-commit>` → `git push origin vX.Y.Z`。
- 现有节奏是 patch 递增（如 `v0.1.5` → `v0.1.6`），tag 打在 PR 的 merge commit 上。

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
