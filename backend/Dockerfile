# Keep mutable upstream tags visible for maintainability, but bind every build
# to the reviewed multi-platform manifest digest.
FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS backend-dependencies

WORKDIR /app

ENV PATH="/opt/venv/bin:${PATH}" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=uv /uv /uvx /bin/

# Resolve only from the committed lock. Application source is deliberately
# copied later so ordinary source changes reuse this expensive dependency layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Browser-free test image for the future sharded backend CI lanes. Test-only
# packages are locked in the dev dependency group rather than injected at run time.
FROM backend-dependencies AS backend-ci
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups --no-install-project
CMD ["python", "-m", "pytest"]

# Browser/runtime dependencies are isolated from the CI image. Publishing this
# target under a dependency hash lets app images reuse it until lock inputs move.
FROM backend-dependencies AS backend-runtime
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg git openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN DEBIAN_FRONTEND=noninteractive playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

FROM backend-runtime AS app
COPY app/ app/
COPY scripts/ scripts/
COPY tests/fixtures/sar_1083_relationship_canary_fixture.json tests/fixtures/sar_1083_relationship_canary_fixture.json
COPY alembic/ alembic/
COPY alembic.ini .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
