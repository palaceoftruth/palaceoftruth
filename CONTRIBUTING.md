# Contributing to Palace of Truth

This guide covers the normal development workflow for humans and agents.

## Repository Structure

```text
backend/                      FastAPI service, ARQ workers, SQLAlchemy models, Alembic migrations
frontend/                     React, TypeScript, Vite, Tailwind web app
extension/                    Chrome-compatible capture extension
chart/                        Portable Helm chart and default public values
argocd/                       Notes for external ArgoCD Application ownership
k8s/                          Notes for external raw manifest ownership
third_party_plugins/           third-party plugin packages
  agent_clients/palaceoftruth-memory/
                                Codex/Claude plugin packaging for the MCP adapter
  hermes/                       canonical Hermes memory plugin source
scripts/                      smoke, benchmark, packaging, migration, and operator helpers
```

## Development Setup

```bash
cp .env.example .env
# Fill in local secrets and model provider keys.
docker compose up --build -d
di up palaceoftruth
open https://palaceoftruth.test
```

Public localhost fallback:

```bash
docker network create traefik 2>/dev/null || true
docker compose -f docker-compose.yml -f docker-compose.localhost.yml up --build -d
open http://localhost:8080
```

Host-side loops are also supported:

```bash
cd backend
pip install -e .
# Requires DATABASE_URL and REDIS_URL from the root .env when running outside Compose.
uvicorn app.main:app --reload
pytest

cd ../frontend
npm install
npm run dev
npm run build

cd ../extension
npm install
npm test
```

## Backend Conventions

- Keep API route handlers in `backend/app/api/`.
- Keep domain logic in `backend/app/services/` or `backend/app/pipelines/`.
- Keep background work in `backend/app/workers/`.
- Use Pydantic schemas for request and response shapes.
- Pair model/schema changes with Alembic migrations under `backend/alembic/versions/`.
- Add tests under `backend/tests/test_<feature>.py`.

## Frontend Conventions

- Keep route pages in `frontend/src/pages/`.
- Keep shared components in `frontend/src/components/`.
- Keep API types and client functions in `frontend/src/api/`.
- Route API calls through `frontend/src/api/client.ts`.
- Verify `npm run build` for frontend changes.
- Exercise changed routes at `https://palaceoftruth.test` when using the standard local stack.

## Deployment And Chart Conventions

- Prefer the Helm chart in `chart/` for deployment changes.
- Keep environment-specific ArgoCD Applications, raw manifests, Helm values overlays, secret-manager item IDs, DNS targets, and private deployment runbooks in a private deployment repository.
- Do not hardcode secrets in manifests or docs.
- Put public build-from-source examples first. Keep private registry paths only in clearly labeled maintainer examples.
- Keep `INTEGRATIONS.md` current when chart values, deployment commands, registry coordinates, or external dependencies change.

## Documentation Conventions

- Keep current operational state in [PROJECT_STATUS.md](PROJECT_STATUS.md).
- Keep portable deployment details in [INTEGRATIONS.md](INTEGRATIONS.md).
- Keep public-facing guidance in top-level Markdown files or package-local README files.
- Avoid absolute local filesystem links in committed docs.

## Verification Before Review

Run the smallest checks that cover the change:

```bash
cd backend
pytest
uv run python ../scripts/check_database_health.py

cd ../frontend
npm run build

cd ../extension
npm test
```

For deployment changes:

```bash
helm lint chart
helm template palaceoftruth chart
helm template palaceoftruth chart --set highAvailability.enabled=true
helm template palaceoftruth chart --set mcp.enabled=true --set ingress.mcpHost=mcp.palaceoftruth.example.com
```

## Pull Requests

PRs should include:

- the user-visible change
- affected API, schema, migration, config, or deployment surfaces
- verification commands and results
- screenshots for UI changes
- any new environment variables or operational requirements

Use Conventional Commits for commit subjects:

```text
feat(api): add memory job retry endpoint
fix(chart): preserve admin ingress annotations
docs(mcp): clarify stdio smoke setup
```

## Public Repository Notes

The planned public repository target is `github.com/palaceoftruth/palaceoftruth`,
but this repository remains the active source until the cutover is announced.
Keep public docs Palace-first, use placeholders for operator-specific
infrastructure, and avoid committing private deployment coordinates except in
clearly labeled maintainer examples.
