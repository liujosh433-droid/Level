# syntax=docker/dockerfile:1.7
# Cloud Run container for the Level API service.
#
# Multi-stage build: builder installs deps into a virtual env, runtime copies
# only the venv + code. Keeps the final image small.

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.5.11

WORKDIR /workspace

# Copy the workspace layout so uv can resolve internal deps.
COPY pyproject.toml uv.lock* ./
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY packages/api/pyproject.toml packages/api/pyproject.toml
COPY packages/jobs/pyproject.toml packages/jobs/pyproject.toml
COPY packages/core/src packages/core/src
COPY packages/api/src packages/api/src
COPY packages/jobs/src packages/jobs/src

RUN uv sync --frozen --no-dev --package level-api

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/workspace/.venv/bin:$PATH"

RUN groupadd --gid 1000 level && useradd --uid 1000 --gid level --shell /bin/bash --create-home level

WORKDIR /workspace
COPY --from=builder /workspace /workspace
RUN chown -R level:level /workspace

USER level
EXPOSE 8080
CMD ["uvicorn", "level_api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
