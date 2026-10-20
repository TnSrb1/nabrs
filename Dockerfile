# Dockerfile — single image of the app stack (ADR-005, ADR-006).
# Python 3.12-slim, dependencies from pyproject.toml. This image runs all
# Compose app services (api/ingest/routing/ai/delivery/bot) and the one-off migrate;
# only the command differs. By default (api) it starts via uvicorn.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Project metadata only first — so that the dependency layer stays cached
# while pyproject.toml does not change. alembic is installed from here as a dependency.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Application sources.
COPY src/ ./src/

# Migration config and scripts — needed by the migrate service (`alembic upgrade head`),
# which runs the same image. setuptools packs only src/, so alembic/
# and alembic.ini are copied explicitly (script_location=alembic, env.py reads $DATABASE_URL).
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Runtime config tier→slug (infra/config.yaml, ADR-029) — needed by AiClient in all
# app services (AI worker L1/L2, bot on-demand L3). Not a secret, committed to the repo.
COPY infra/config.yaml ./infra/config.yaml

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
