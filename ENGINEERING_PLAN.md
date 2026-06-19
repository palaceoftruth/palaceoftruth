# palaceoftruth — Engineering Plan Archive

`ENGINEERING_PLAN.md` is now archival context for the original MVP design and early architecture assumptions.

For the current project state, use:

- [PROJECT_STATUS.md](./PROJECT_STATUS.md) for shipped / in progress / next
- [INTEGRATIONS.md](./INTEGRATIONS.md) for deployment and integration details
- [TODOS.md](./TODOS.md) for active follow-up work

If the overall architecture changes materially, update `PROJECT_STATUS.md` and the relevant operational docs first. Keep this file only as historical reference unless the archive itself needs cleanup.

---

## Historical Content Starts Here

Everything below this point is preserved from the original MVP engineering plan. It is not an active task list and may describe behavior that has since shipped, changed, or been superseded.
- Store summary on the item

### Auto-Tagging & Categorization
- Send content + existing tag vocabulary to OpenRouter
- Prompt: "Generate 3-7 tags and 1-3 categories. Prefer existing tags when relevant: [list]"
- This keeps the tag space from exploding while still allowing new tags

### Relationship Extraction
- After a new item is ingested, compare its embedding centroid to existing items
- For the top-N most similar existing items, send both summaries to OpenRouter
- Prompt: "What is the relationship? Options: related_to, contradicts, expands_on, prerequisite_of, example_of"
- Store relationships with confidence scores

---

## Project Structure

```
palaceoftruth/
├── docker-compose.yml
├── .env.example
├── README.md
│
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic/                    # DB migrations
│   │   ├── alembic.ini
│   │   └── versions/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI app, middleware, startup
│   │   ├── config.py               # Settings from env vars (pydantic-settings)
│   │   ├── auth.py                 # API key validation
│   │   ├── database.py             # SQLAlchemy async engine + session
│   │   │
│   │   ├── models/                 # SQLAlchemy ORM models
│   │   │   ├── item.py
│   │   │   ├── embedding.py
│   │   │   ├── relationship.py
│   │   │   └── job.py
│   │   │
│   │   ├── schemas/                # Pydantic request/response schemas
│   │   │   ├── ingest.py
│   │   │   ├── search.py
│   │   │   ├── chat.py
│   │   │   ├── item.py
│   │   │   └── job.py
│   │   │
│   │   ├── api/                    # Route handlers
│   │   │   ├── ingest.py
│   │   │   ├── search.py
│   │   │   ├── chat.py
│   │   │   ├── items.py
│   │   │   ├── jobs.py
│   │   │   └── system.py
│   │   │
│   │   ├── services/               # Business logic
│   │   │   ├── chunker.py          # Semantic text chunking
│   │   │   ├── embedder.py         # OpenAI embedding calls
│   │   │   ├── llm.py              # OpenRouter chat/summarization calls
│   │   │   ├── search.py           # Vector + full-text search
│   │   │   └── relationships.py    # Relationship extraction
│   │   │
│   │   ├── pipelines/              # Ingestion pipelines
│   │   │   ├── base.py             # Abstract pipeline class
│   │   │   ├── youtube.py
│   │   │   ├── webpage.py
│   │   │   ├── pdf.py
│   │   │   └── note.py
│   │   │
│   │   └── workers/                # ARQ worker definitions
│   │       ├── worker.py           # ARQ worker config + startup
│   │       └── tasks.py            # Task definitions (one per pipeline)
│   │
│   └── tests/
│       ├── conftest.py
│       ├── test_api/
│       ├── test_pipelines/
│       └── test_services/
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── api/                    # API client (fetch wrapper)
│       │   └── client.ts
│       ├── components/
│       │   ├── Layout.tsx
│       │   ├── SearchBar.tsx
│       │   ├── ItemCard.tsx
│       │   ├── ItemDetail.tsx
│       │   ├── ChatPanel.tsx
│       │   ├── IngestForm.tsx
│       │   ├── JobStatus.tsx
│       │   └── TagCloud.tsx
│       ├── pages/
│       │   ├── Dashboard.tsx       # Overview: recent items, stats
│       │   ├── Search.tsx          # Vector search + filters
│       │   ├── Chat.tsx            # RAG chat interface
│       │   ├── Browse.tsx          # Browse/filter all items
│       │   ├── ItemView.tsx        # Single item detail + related
│       │   └── Ingest.tsx          # Add new content
│       └── hooks/
│           ├── useSearch.ts
│           ├── useChat.ts
│           └── useItems.ts
│
└── k8s/                            # Future: Kubernetes manifests
    ├── namespace.yaml
    ├── postgres.yaml
    ├── redis.yaml
    ├── backend.yaml
    ├── worker.yaml
    ├── frontend.yaml
    └── ingress.yaml
```

---

## Docker Compose (MVP)

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: palaceoftruth
      POSTGRES_USER: palaceoftruth
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U palaceoftruth"]
      interval: 5s

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s

  backend:
    build: ./backend
    environment:
      DATABASE_URL: postgresql+asyncpg://palaceoftruth:${DB_PASSWORD}@postgres:5432/palaceoftruth
      REDIS_URL: redis://redis:6379
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}
      API_KEY: ${API_KEY}
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - temp_files:/tmp/palaceoftruth

  worker:
    build: ./backend
    command: arq app.workers.worker.WorkerSettings
    environment:
      DATABASE_URL: postgresql+asyncpg://palaceoftruth:${DB_PASSWORD}@postgres:5432/palaceoftruth
      REDIS_URL: redis://redis:6379
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - temp_files:/tmp/palaceoftruth

  frontend:
    build: ./frontend
    ports:
      - "3000:80"
    depends_on:
      - backend

volumes:
  pgdata:
  temp_files:
```

---

## MVP Build Phases

### Phase 1: Foundation (Days 1-3)
- [ ] Initialize project structure (backend + frontend scaffolds)
- [ ] Docker Compose with PostgreSQL (pgvector) + Redis
- [ ] FastAPI app skeleton with health check, API key auth
- [ ] SQLAlchemy models + Alembic migrations
- [ ] ARQ worker setup with a test task
- [ ] Basic Pydantic schemas

### Phase 2: Core Ingestion (Days 4-7)
- [ ] Text chunking service (semantic chunking with overlap)
- [ ] OpenAI embedding service
- [ ] OpenAI LLM service (summary, tags, categories)
- [ ] YouTube pipeline (yt-dlp → Whisper → chunk → embed → store)
- [ ] Web page pipeline (trafilatura → chunk → embed → store)
- [ ] PDF pipeline (pypdf → chunk → embed → store)
- [ ] Note pipeline (direct text → chunk → embed → store)
- [ ] Job tracking (status, progress, errors)

### Phase 3: Search & Chat (Days 8-10)
- [ ] Vector search endpoint (pgvector cosine similarity)
- [ ] Hybrid search (vector + full-text with pg_trgm)
- [ ] Filters: source type, tags, date range
- [ ] RAG chat endpoint with source citations
- [ ] Chat history support (multi-turn)

### Phase 4: AI Enrichment (Days 11-12)
- [ ] Auto-summarization in pipelines
- [ ] Auto-tagging with vocabulary awareness
- [ ] Auto-categorization
- [ ] Relationship extraction between items

### Phase 5: Web UI (Days 13-17)
- [ ] React + Vite + Tailwind setup
- [ ] Dashboard page (stats, recent items)
- [ ] Ingest page (URL input, file upload, note editor)
- [ ] Search page (query + filters + results)
- [ ] Chat page (conversational RAG interface)
- [ ] Browse page (all items, filter/sort)
- [ ] Item detail page (content, summary, tags, related items)
- [ ] Job status indicators

### Phase 6: Polish & Deploy (Days 18-20)
- [ ] Error handling and retry logic in pipelines
- [ ] Rate limiting on OpenAI/OpenRouter calls (with fallback model rotation)
- [ ] API documentation (auto-generated OpenAPI/Swagger)
- [ ] Docker builds optimized (multi-stage)
- [ ] Environment variable documentation
- [ ] Basic test suite (API endpoints, pipeline unit tests)
- [ ] README with setup instructions

---

## Key Dependencies (Python)

```toml
[project]
name = "palaceoftruth"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "pgvector>=0.3",
    "arq>=0.26",
    "redis>=5.0",
    "openai>=1.40",
    "yt-dlp>=2024.0",
    "trafilatura>=1.12",
    "pypdf>=4.0",
    "pdfplumber>=0.11",
    "pydantic-settings>=2.0",
    "python-multipart>=0.0.9",
    "httpx>=0.27",
    "tiktoken>=0.7",           # Token counting for chunking
]
```

---

## Configuration (.env)

```bash
# Database
DB_PASSWORD=your_secure_password

# OpenAI (embeddings + transcription only)
OPENAI_API_KEY=sk-...

# OpenRouter (LLM — chat, summarization, tagging)
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_DEFAULT_MODEL=minimax/minimax-m2.7
OPENROUTER_FALLBACK_MODELS=nvidia/nemotron-3-super-120b-a12b

# API Auth
API_KEY=your_api_key_here

# Optional tuning
EMBEDDING_MODEL=text-embedding-3-large      # OpenAI embeddings
WHISPER_MODEL=whisper-1                     # OpenAI transcription
CHUNK_SIZE=500          # tokens per chunk
CHUNK_OVERLAP=50        # overlap tokens
SEARCH_LIMIT=10         # default search results
```

---

## Future Enhancements (Post-MVP)

- **More sources**: Kindle highlights, Twitter bookmarks, podcast RSS, email newsletters, Slack messages
- **Browser extension**: One-click "save to palaceoftruth" from any page
- **Scheduled ingestion**: Auto-ingest from RSS feeds, YouTube channels, etc.
- **Knowledge graph visualization**: D3.js graph of item relationships
- **Export**: Export knowledge base as markdown, JSON, or PDF
- **Multi-model support**: Swap in local models (Ollama) for offline/privacy, or upgrade to paid OpenRouter models
- **Kubernetes manifests**: Helm chart for RKE2 deployment
- **Webhooks**: Notify external tools when new items are ingested
- **Deduplication**: Detect and merge duplicate content
- **Full-text search**: PostgreSQL tsvector for keyword search alongside vector search
