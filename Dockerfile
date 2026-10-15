# syntax=docker/dockerfile:1

# ---- UI build -------------------------------------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite writes to ../backend/static
RUN npm run build

# ---- Python dependencies --------------------------------------------------------------------
FROM python:3.13-slim AS deps
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- Runtime --------------------------------------------------------------------------------
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    DATA_DIR=/data
WORKDIR /app
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
COPY --from=deps /app/.venv /app/.venv
COPY backend/app ./app
COPY --from=ui /src/backend/static ./static
USER app
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "app.main"]
