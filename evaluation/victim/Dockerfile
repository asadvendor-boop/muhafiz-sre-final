# ═══════════════════════════════════════════════════════════════════════════════
# MuhafizSRE Victim Service — Simulated auth-service for Sandbox Testing
# ═══════════════════════════════════════════════════════════════════════════════
# Lightweight container that runs a FastAPI app on port 9000.
# Used by the evaluation harness to simulate fault injection and recovery.
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.13-slim

WORKDIR /app

# Install only the minimal dependencies required
RUN pip install --no-cache-dir fastapi uvicorn

# Copy application source
COPY app.py .

# Run as non-root for security
RUN groupadd -r victim && useradd -r -g victim victim
USER victim

EXPOSE 9000

HEALTHCHECK --interval=10s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9000/health')" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
