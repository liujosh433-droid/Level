FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LEVEL_ENV=cloud

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY packages/core /app/packages/core
COPY packages/api  /app/packages/api

# Demo-mode fixtures ship inside the level_core wheel via
# packages/core/src/level_core/demo/data/*.ics + importlib.resources,
# so no separate example-data COPY is needed here. See
# ``level_core.demo.scenarios.ScenarioConfig.ics_path`` for the
# runtime lookup, and pyproject.toml's [tool.hatch.build.targets.
# wheel.force-include] for the packaging fence.

RUN uv pip install --system --no-cache /app/packages/core /app/packages/api

EXPOSE 8080

CMD ["uvicorn", "level_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
