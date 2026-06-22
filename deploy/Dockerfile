# conduit-service 镜像（跑在 rig）。core(conduit) + service 一起装。
# 构建上下文 = 仓库根（见 deploy/compose.yaml）。镜像里不带 db/secrets（见 .dockerignore）。
FROM python:3.12-slim

WORKDIR /app
ARG CONDUIT_VERSION=0.0.0
COPY pyproject.toml ./
COPY conduit/ ./conduit/
COPY service/ ./service/
RUN pip install --no-cache-dir '.[service]' && mkdir -p /data

ENV CONDUIT_DB=/data/conduit.db
ENV CONDUIT_VERSION=${CONDUIT_VERSION}
EXPOSE 8000

# 容器内绑 0.0.0.0（隔离 netns）；对外暴露由 compose 的端口映射控制（默认只到宿主 localhost）。
# --no-access-log：订阅 token 在 URL query 里，关掉访问日志避免 token 进日志。
CMD ["uvicorn", "--factory", "service.app:make_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
