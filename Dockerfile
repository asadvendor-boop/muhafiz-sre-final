# ═══════════════════════════════════════════════════════════════════════════════
# MuhafizSRE: Autonomous Incident Guardian
# Multi-Stage Dockerfile — Optimized for Google Cloud Run
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Stage 1: Builder ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv for fast, reproducible dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency manifests first for layer caching
COPY pyproject.toml uv.lock README.md ./
COPY agents/ ./agents/
COPY gateway/ ./gateway/
COPY shared/ ./shared/
COPY evaluation/ ./evaluation/

# Install with frozen lockfile — reproducible builds
RUN uv sync --frozen --all-extras --no-dev

# ─── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy the pre-built virtualenv from builder stage
COPY --from=builder /build/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application source code
COPY agents/ ./agents/
COPY gateway/ ./gateway/
COPY shared/ ./shared/
COPY evaluation/ ./evaluation/

# Security: Create non-root user and switch to it
RUN groupadd -r muhafiz && \
    useradd -r -g muhafiz -d /app -s /sbin/nologin muhafiz && \
    mkdir -p /data && \
    chown -R muhafiz:muhafiz /app && \
    chown -R muhafiz:muhafiz /data

USER muhafiz

# Expose the Gateway API port
EXPOSE 8000

# Health check for Cloud Run
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Start the FastAPI Gateway server
CMD ["python", "-m", "uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
