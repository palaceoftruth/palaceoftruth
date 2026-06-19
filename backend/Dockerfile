FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y ffmpeg git openssh-client && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
RUN pip install --no-cache-dir .
# Install Chromium + OS-level dependencies for Playwright
RUN playwright install --with-deps chromium
COPY app/ app/
COPY scripts/ scripts/
COPY alembic/ alembic/
COPY alembic.ini .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
