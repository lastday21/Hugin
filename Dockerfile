# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

RUN python -m pip install --no-cache-dir uv==0.11.28

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="Hugin" \
      org.opencontainers.image.description="Локальный API персонального агента поиска работы" \
      org.opencontainers.image.source="https://github.com/lastday21/Hugin"

ENV HUGIN_API_HOST=0.0.0.0 \
    HUGIN_API_PORT=8000 \
    HUGIN_DATA_DIR=/data \
    HUGIN_ENVIRONMENT=production \
    HOME=/home/hugin \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 hugin \
    && useradd --uid 10001 --gid hugin --create-home --shell /usr/sbin/nologin hugin \
    && mkdir --parents /app /data \
    && chown --recursive hugin:hugin /app /data

WORKDIR /app

COPY --from=builder --chown=hugin:hugin /app/.venv /app/.venv

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).close()"]

STOPSIGNAL SIGTERM

CMD ["hugin"]
