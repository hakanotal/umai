# Built by Railway from the connected repo, and on the Mac by
# docker-compose.app.yml when you want to watch the real image start before
# pushing. No architecture is pinned here: `just build`'s old --platform
# linux/arm64 existed only because the Pi was arm64.
#
# No `--mount=type=cache` on the uv steps, deliberately. Railway's builder
# rejects the flag — "missing an id argument", and adding an id does not
# satisfy it either — and the rejection lands before the build starts, so the
# deploy fails with a build log containing nothing but "scheduling build",
# which reads like a broken builder rather than a bad Dockerfile. The cost is
# small: dependencies are still their own layer, copied before the source, so
# the only builds that pay full download are the ones that changed uv.lock.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# dependencies first, so a source edit does not invalidate the layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY README.md ./
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------

FROM python:3.13-slim-bookworm

RUN useradd --create-home --uid 1000 umai
WORKDIR /app

COPY --from=builder --chown=umai:umai /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER umai
EXPOSE 8000

CMD ["python", "-m", "umai"]
