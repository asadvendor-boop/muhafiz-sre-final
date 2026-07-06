#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# MuhafizSRE — Single Container Entrypoint
# Starts both the FastAPI gateway and Next.js dashboard in one container.
# Cloud Run exposes port $PORT (default 3000) → Next.js → proxies /api/* → FastAPI
# ═══════════════════════════════════════════════════════════════════════════════

set -e

GATEWAY_PORT=8000
DASHBOARD_PORT="${PORT:-3000}"

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  MuhafizSRE — Starting Gateway + Dashboard                  ║"
echo "║  Gateway:   http://localhost:${GATEWAY_PORT}                         ║"
echo "║  Dashboard: http://localhost:${DASHBOARD_PORT} (Cloud Run PORT)       ║"
echo "╚═══════════════════════════════════════════════════════════════╝"

# ── SQLite on GCS FUSE: use local /tmp with periodic sync ──
# GCS FUSE doesn't support SQLite's random writes well, so we use /tmp
# and copy to /data on shutdown for persistence
if [ -f /data/muhafiz.db ]; then
  echo "[entrypoint] Restoring SQLite DB from GCS FUSE..."
  cp /data/muhafiz.db /tmp/muhafiz.db
  echo "[entrypoint] DB restored ✓"
else
  echo "[entrypoint] No existing DB, starting fresh"
fi
export MUHAFIZ_DB_PATH=/tmp/muhafiz.db

# ── Background sync: periodically back up DB to GCS ──
sync_db() {
  while true; do
    sleep 60
    if [ -f /tmp/muhafiz.db ]; then
      cp /tmp/muhafiz.db /data/muhafiz.db 2>/dev/null || true
    fi
  done
}
sync_db &
SYNC_PID=$!

# ── Start the FastAPI Gateway (background) ──
echo "[entrypoint] Starting FastAPI gateway on port ${GATEWAY_PORT}..."
cd /app
python -m uvicorn gateway.app:app \
  --host 0.0.0.0 \
  --port "${GATEWAY_PORT}" \
  --log-level info \
  --no-access-log &
GATEWAY_PID=$!

# ── Wait for gateway to be healthy ──
echo "[entrypoint] Waiting for gateway health..."
for i in $(seq 1 30); do
  if python -c "import urllib.request; urllib.request.urlopen('http://localhost:${GATEWAY_PORT}/health')" 2>/dev/null; then
    echo "[entrypoint] Gateway healthy ✓"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "[entrypoint] ERROR: Gateway failed to start"
    exit 1
  fi
  sleep 1
done

# ── Start the Next.js Dashboard (foreground) ──
echo "[entrypoint] Starting Next.js dashboard on port ${DASHBOARD_PORT}..."
cd /app/dashboard
export HOSTNAME="0.0.0.0"
export PORT="${DASHBOARD_PORT}"
node server.js &
DASHBOARD_PID=$!

# ── Wait for dashboard to be ready ──
echo "[entrypoint] Waiting for dashboard health..."
for i in $(seq 1 30); do
  if node -e "fetch('http://localhost:${DASHBOARD_PORT}').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))" 2>/dev/null; then
    echo "[entrypoint] Dashboard healthy ✓"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "[entrypoint] WARNING: Dashboard health check timed out, continuing..."
  fi
  sleep 1
done

echo "[entrypoint] ✅ Both services running"

# ── Handle shutdown gracefully ──
cleanup() {
  echo "[entrypoint] Shutting down, syncing DB to GCS..."
  cp /tmp/muhafiz.db /data/muhafiz.db 2>/dev/null || true
  kill $GATEWAY_PID $DASHBOARD_PID $SYNC_PID 2>/dev/null
  exit 0
}
trap cleanup SIGTERM SIGINT

# ── Wait for any process to exit ──
wait $GATEWAY_PID $DASHBOARD_PID
