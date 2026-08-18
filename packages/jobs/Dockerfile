FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LEVEL_ENV=cloud

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY packages/core /app/packages/core
COPY packages/jobs /app/packages/jobs

RUN uv pip install --system --no-cache /app/packages/core /app/packages/jobs

CMD ["python", "-m", "level_jobs.nightly"]
