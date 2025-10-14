# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# System dependencies required for PDF processing and health checks
RUN apt-get update \ 
    && apt-get install -y --no-install-recommends \
        build-essential \
        ghostscript \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies first to leverage Docker layer caching
COPY pyproject.toml poetry.lock* README.md ./
RUN pip install --upgrade pip setuptools wheel

# Copy application source
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail http://localhost:${PORT}/docs || exit 1

CMD ["sh", "-c", "uvicorn pdf_analysis.api.server:app --host 0.0.0.0 --port ${PORT}"]
