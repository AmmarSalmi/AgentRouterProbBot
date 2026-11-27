FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50

ARG TELEGRAM_OWNERSHIP_CONFIGURED
RUN test -n "$TELEGRAM_OWNERSHIP_CONFIGURED" || { \
    echo "Set OWNERSHIP_OTP or TELEGRAM_CHAT_ID in the project .env before building." >&2; \
    exit 1; \
    }

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_PATH=/data/state.json

WORKDIR /app

RUN groupadd --system --gid 999 prober \
    && useradd --system --uid 999 --gid prober --home-dir /app prober \
    && mkdir -p /data \
    && chown prober:prober /data

COPY requirements-docker.txt ./
RUN pip install --require-hashes --only-binary=:all: -r requirements-docker.txt

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-index --no-build-isolation --no-deps .

USER prober

VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD ["python", "-m", "agentrouter_prober", "--healthcheck"]

ENTRYPOINT ["python", "-m", "agentrouter_prober"]

