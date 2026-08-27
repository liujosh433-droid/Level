FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LEVEL_ENV=cloud

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY packages/core /app/packages/core
COPY packages/api  /app/packages/api

# Demo-mode fixtures. seeder.py resolves ICS paths by walking up from
# the demo module looking for a directory containing `example-data/` -
# without this COPY the walk falls off /app and /v1/auth/demo returns
# demo_ics_missing. Kept as a plain directory copy (not moved inside
# the package) so contributors can still eyeball the ICS files at the
# repo root and the ICS generator writes to its canonical location.
COPY example-data /app/example-data

RUN uv pip install --system --no-cache /app/packages/core /app/packages/api

EXPOSE 8080

CMD ["uvicorn", "level_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
