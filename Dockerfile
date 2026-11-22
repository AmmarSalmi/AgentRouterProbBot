FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_PATH=/data/state.json

WORKDIR /app

RUN groupadd --system prober \
    && useradd --system --gid prober --home-dir /app prober \
    && mkdir -p /data \
    && chown prober:prober /data

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install .

USER prober

VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD ["python", "-m", "agentrouter_prober", "--healthcheck"]

ENTRYPOINT ["python", "-m", "agentrouter_prober"]

