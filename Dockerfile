# ── Build frontend ────────────────────────────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# System deps for PyMuPDF and psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmupdf-dev gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

# Frontend build output
COPY --from=frontend /app/web/dist /app/web/dist

# Default storage directory
RUN mkdir -p /app/storage

ENV OPENCRAIG_HOST=0.0.0.0
ENV OPENCRAIG_PORT=8000

EXPOSE 8000

CMD ["python", "main.py", "--workers", "4"]
