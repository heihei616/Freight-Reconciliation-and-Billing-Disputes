# ============================================================
# 生产镜像：多阶段构建 + 非 root 运行
#
# 为什么不把源码和依赖装在一个阶段：
# 编译 wheel 需要 gcc 等工具链，运行阶段完全用不到。
# 多阶段把工具链留在 builder，最终镜像只带 venv 与源码，
# 体积更小，攻击面也更小。
# ============================================================
FROM python:3.12-slim AS builder

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


# ============================================================
# 运行阶段
# ============================================================
FROM python:3.12-slim

LABEL org.opencontainers.image.title="logistics-agent" \
      org.opencontainers.image.description="物流费对账与计费争议智能体" \
      org.opencontainers.image.licenses="MIT"

# libgomp1 是 faiss-cpu 的 OpenMP 运行时依赖，slim 镜像默认没有；
# 少了它会在 import faiss 时直接报 libgomp.so.1 not found。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY code/src/ ./src/
COPY config/ ./config/
COPY data/knowledge/ ./data/knowledge/
COPY code/scripts/preflight.py code/scripts/backup_db.py ./scripts/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SERVER_HOST=0.0.0.0 \
    LOGISTICS_DB_PATH=/app/data/runtime/chat.db \
    RAG_INDEX_DIR=/app/data/index

# 运行目录归 app 用户；数据库卷挂到 /app/data，首次启动会自动建表。
RUN mkdir -p /app/data/runtime /app/data/index \
    && chown -R app:app /app

USER app
EXPOSE 8000

# 用 /healthz 而不是 /readyz：容器健康检查决定「要不要重启」，
# 索引重建中不该被杀掉，那属于「暂不接流量」而非「进程坏了」。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).getcode() == 200 else 1)"

# SIGTERM 交给 uvicorn：它会先停止接收新请求，再等待在途请求结束。
# compose 里配了 stop_grace_period，给 SSE 长连接留出收尾时间。
CMD ["python", "-m", "logistics_agent.main"]
