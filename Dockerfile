# Built on the Mac for linux/arm64, pushed, pulled by the Pi. Never built on the
# Pi: it is slow, and it puts a compiler toolchain on the machine holding the data.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# dependencies first, so a source edit does not invalidate the layer
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

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
