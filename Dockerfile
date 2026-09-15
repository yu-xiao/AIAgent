FROM node:22.15.0-alpine AS web-builder

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.9.21@sha256:15f68a476b768083505fe1dbfcc998344d0135f0ca1b8465c4760b323904f05a AS uv
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS builder

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS runtime

ARG VERSION=0.3.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Enterprise AI Agent" \
      org.opencontainers.image.description="Enterprise AI agent platform" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.licenses="Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system aiagent && useradd --system --gid aiagent --home /app aiagent
WORKDIR /app
COPY --from=builder --chown=aiagent:aiagent /app/.venv /app/.venv
COPY --chown=aiagent:aiagent alembic ./alembic
COPY --chown=aiagent:aiagent alembic.ini ./alembic.ini
COPY --from=web-builder --chown=aiagent:aiagent /web/dist ./web/dist

USER aiagent
EXPOSE 8000
CMD ["ai-agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
