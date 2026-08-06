# syntax=docker/dockerfile:1

# --- frontend build -------------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY vite.config.js ./
COPY frontend ./frontend
RUN npx vite build

# --- backend runtime ------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERIN_BACKEND_HOST=0.0.0.0 \
    SERIN_BACKEND_PORT=8890 \
    SERIN_DB_PATH=/data/serin.db

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "uvicorn[standard]"

COPY backend ./backend
COPY docs/CONNECTORS.md docs/PRIVACY-POLICY.md docs/TERMS.md docs/DEPLOY.md ./docs/
# Served at /security by the policy-page routes, alongside the docs above.
COPY SECURITY.md ./SECURITY.md
COPY --from=frontend /app/frontend/dist ./frontend/dist
# main.py mounts the built SPA from REPO_ROOT/frontend/dist and serves it
# alongside the API, so one container hosts everything.

# Run as a non-root user; /data holds the SQLite DB + secrets key file.
RUN useradd --create-home --uid 10001 serin \
 && mkdir -p /data /app/logs \
 && chown -R serin:serin /data /app
USER serin

EXPOSE 8890
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8890/api/v1/version || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8890"]
