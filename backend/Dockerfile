# Keep mutable upstream tags visible for maintainability, but bind every build
# to the reviewed multi-platform manifest digest.
FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS backend-builder

WORKDIR /app
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# CI is not a deployable runtime image. It keeps uv only to run the locked test
# groups without adding those packages to an application image.
FROM backend-builder AS backend-ci
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups --no-install-project
CMD ["python", "-m", "pytest"]

FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime-base
WORKDIR /app
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/palace
COPY --from=backend-builder /opt/venv /opt/venv
RUN groupadd --gid 10001 palace \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/palace palace \
    && mkdir -p /tmp/palaceoftruth \
    && chown palace:palace /tmp/palaceoftruth /home/palace

# The API, MCP adapter, migrations, and release hooks use this lean stage. It
# does not contain uv, git, ssh, ffmpeg, Chromium, or build tools.
FROM runtime-base AS backend-runtime

# Only queue workers process media and browser-backed captures. Keep those
# feature tools in the worker image instead of every public API pod.
FROM runtime-base AS worker-runtime
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg git openssh-client \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright \
    && rm -rf /var/lib/apt/lists/*
COPY app/ app/
COPY scripts/ scripts/
COPY tests/fixtures/sar_1083_relationship_canary_fixture.json tests/fixtures/sar_1083_relationship_canary_fixture.json
COPY alembic/ alembic/
COPY alembic.ini .
RUN python -m compileall -q app scripts alembic
USER 10001:10001
CMD ["arq", "app.workers.worker.WorkerSettings"]

FROM worker-runtime AS worker

# Keep app last so plain `docker build` and Compose default to the lean image.
FROM backend-runtime AS app
COPY app/ app/
COPY scripts/ scripts/
COPY tests/fixtures/sar_1083_relationship_canary_fixture.json tests/fixtures/sar_1083_relationship_canary_fixture.json
COPY alembic/ alembic/
COPY alembic.ini .
RUN python -m compileall -q app scripts alembic
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
