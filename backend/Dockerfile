# Keep mutable upstream tags visible for maintainability, but bind every build
# to the reviewed multi-platform manifest digest.
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv
FROM denoland/deno:bin-2.9.5@sha256:0d1262facd139e815217c001945eb822c7a78584cf660142c34a6b53effec1aa AS deno
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
# Apply Debian security updates the pinned base image predates. The image
# digest stays pinned for reproducibility, so without this the backend and
# worker images keep shipping whatever was current when that digest was cut.
# Trivy runs with ignore-unfixed, so it fails the build on exactly the
# packages this upgrade can fix, which is why upgrading beats a suppression.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/palace
COPY --from=backend-builder /opt/venv /opt/venv
RUN groupadd --gid 10001 palace \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/palace palace \
    && mkdir -p /tmp/palaceoftruth \
    && chown palace:palace /tmp/palaceoftruth /home/palace

# The API, MCP adapter, migrations, and release hooks use this lean stage.
# Inline repository sync is an API contract, so it keeps the Git and SSH
# clients. Media tools, Chromium, uv, and build tools remain worker-only.
FROM runtime-base AS backend-runtime
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Only queue workers process media and browser-backed captures. Keep those
# feature tools in the worker image instead of every public API pod.
FROM runtime-base AS worker-runtime
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
COPY --from=deno /deno /usr/local/bin/deno
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
RUN deno --version \
    && python -c "import yt_dlp_ejs" \
    && python -m compileall -q app scripts alembic
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
