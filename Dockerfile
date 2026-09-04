FROM ghcr.io/astral-sh/uv:0.9.21 AS uv
FROM python:3.12-slim AS builder

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system aiagent && useradd --system --gid aiagent --home /app aiagent
WORKDIR /app
COPY --from=builder --chown=aiagent:aiagent /app/.venv /app/.venv
COPY --chown=aiagent:aiagent alembic ./alembic
COPY --chown=aiagent:aiagent alembic.ini ./alembic.ini

USER aiagent
EXPOSE 8000
CMD ["ai-agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
