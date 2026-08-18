FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LEVEL_ENV=cloud

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY packages/core /app/packages/core
COPY packages/api  /app/packages/api

RUN uv pip install --system --no-cache /app/packages/core /app/packages/api

EXPOSE 8080

CMD ["uvicorn", "level_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
