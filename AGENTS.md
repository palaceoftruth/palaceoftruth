# Repository Guidelines

## Project Structure & Module Organization
`backend/` contains the FastAPI service, ARQ worker code, SQLAlchemy models, ingestion pipelines, and Alembic migrations. Keep API routes in `backend/app/api/`, domain logic in `backend/app/services/`, queue jobs in `backend/app/workers/`, and schema/model changes paired with a migration in `backend/alembic/versions/`.

`frontend/` is a Vite + React + TypeScript app. Route pages live in `frontend/src/pages/`, shared UI in `frontend/src/components/`, API types and client code in `frontend/src/api/`, and reusable hooks in `frontend/src/hooks/`. The portable Helm chart lives in `chart/`; environment-specific deployment overlays belong outside this public app repo. Current public-facing guidance lives in the top-level Markdown files.

## Build, Test, and Development Commands
Standard local development runs through Docker Compose plus devinfra:

```bash
cp .env.example .env
docker compose up --build -d
di up palaceoftruth
# Standard local review target
open https://palaceoftruth.test
```

Frontend fast iteration remains available when you want host-side HMR:

```bash
cd frontend
npm install
npm run dev
npm run build
```

Host-run Vite is a fast loop, not the standard review target. It proxies `/api` to
`https://api.palaceoftruth.test` by default so browser sessions still use the devinfra
API path.

Backend local development:

```bash
cd backend
pip install -e .
uvicorn app.main:app --reload
pytest
```

Start the async worker when testing ingestion or feed jobs:

```bash
cd backend
arq app.workers.worker.WorkerSettings
```

## Coding Style & Naming Conventions
Follow the existing style rather than introducing a new formatter. Python uses 4-space indentation, type-aware FastAPI modules, and snake_case filenames like `feed_tasks.py`. React/TypeScript uses 2-space indentation, PascalCase components like `ItemCard.tsx`, and camelCase hooks/utilities like `useJobPoller.ts`.

Keep modules narrow: API handlers should stay thin, while orchestration belongs in `services/` or `pipelines/`. Tailwind utility usage is already enabled in `frontend/src`.

## Testing Guidelines
Backend tests use `pytest` under `backend/tests/`. Name files `test_<feature>.py` and keep fixtures in `conftest.py` when shared. Add tests with every API, worker, or migration-affecting change; the current suite is light, so new behavior should not ship without coverage.

Frontend Playwright coverage exists but is not enforced in CI yet. For UI
changes, verify `npm run build`, run targeted `npm run test:e2e` coverage when
applicable, and exercise the affected route at `https://palaceoftruth.test`.

## Commit & Pull Request Guidelines
Recent history follows Conventional Commits with scopes, for example `feat(api): ...`, `fix(chart): ...`, and `chore(ci): ...`. Keep subjects imperative and concise; include `[skip ci]` only for release automation updates.

PRs should describe the user-visible change, call out config or migration impacts, link the relevant issue or plan doc, and include screenshots for frontend changes. Note any new environment variables by updating `.env.example`.
