#!/usr/bin/env bash
set -euo pipefail

# Start-only script: backend + frontend + Cloudflare quick tunnel
# Assumes dependencies are already installed.
# Usage:
#   bash scripts/run_server_and_tunnel.sh
# Optional env:
#   BACKEND_HOST=127.0.0.1 BACKEND_PORT=8000 FRONTEND_HOST=127.0.0.1 FRONTEND_PORT=5173
#   OMNIVOICE_DEVICE=cuda OMNIVOICE_DTYPE=auto OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=2

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
OMNIVOICE_DTYPE="${OMNIVOICE_DTYPE:-auto}"
OMNIVOICE_MAX_CONCURRENT_LONG_JOBS="${OMNIVOICE_MAX_CONCURRENT_LONG_JOBS:-2}"

if [[ -z "${OMNIVOICE_DEVICE:-}" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    OMNIVOICE_DEVICE="cuda"
  else
    OMNIVOICE_DEVICE="cpu"
  fi
fi

export OMNIVOICE_DEVICE
export OMNIVOICE_DTYPE
export OMNIVOICE_MAX_CONCURRENT_LONG_JOBS
export HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-1}"

# Python command resolution
PYTHON_CMD=""
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_CMD=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
elif command -v py >/dev/null 2>&1; then
  PYTHON_CMD="py"
else
  echo "[ERROR] No Python command found (.venv/bin/python | python3 | python | py)"
  exit 1
fi

command -v npm >/dev/null 2>&1 || { echo "[ERROR] npm not found"; exit 1; }
command -v cloudflared >/dev/null 2>&1 || { echo "[ERROR] cloudflared not found"; exit 1; }

echo "[INFO] ROOT_DIR=$ROOT_DIR"
echo "[INFO] PYTHON_CMD=$PYTHON_CMD"
echo "[INFO] OMNIVOICE_DEVICE=$OMNIVOICE_DEVICE"
echo "[INFO] OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=$OMNIVOICE_MAX_CONCURRENT_LONG_JOBS"

echo "[STEP] Starting backend ${BACKEND_HOST}:${BACKEND_PORT} ..."
"$PYTHON_CMD" -m uvicorn server.api:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" &
BACKEND_PID=$!

echo "[STEP] Starting frontend ${FRONTEND_HOST}:${FRONTEND_PORT} ..."
pushd frontend >/dev/null
npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
FRONTEND_PID=$!
popd >/dev/null

CLOUDFLARE_LOG="/tmp/omnivoice_cloudflared.log"
rm -f "$CLOUDFLARE_LOG"

echo "[STEP] Starting Cloudflare quick tunnel ..."
cloudflared tunnel --url "http://${FRONTEND_HOST}:${FRONTEND_PORT}" > "$CLOUDFLARE_LOG" 2>&1 &
CLOUDFLARED_PID=$!

cleanup() {
  echo ""
  echo "[INFO] Stopping services..."
  kill "$CLOUDFLARED_PID" >/dev/null 2>&1 || true
  kill "$FRONTEND_PID" >/dev/null 2>&1 || true
  kill "$BACKEND_PID" >/dev/null 2>&1 || true
  wait "$CLOUDFLARED_PID" >/dev/null 2>&1 || true
  wait "$FRONTEND_PID" >/dev/null 2>&1 || true
  wait "$BACKEND_PID" >/dev/null 2>&1 || true
  echo "[INFO] Done."
}

trap cleanup INT TERM EXIT

# Wait up to 20s for tunnel URL
for _ in $(seq 1 40); do
  if grep -Eo 'https://[-a-zA-Z0-9]+\.trycloudflare\.com' "$CLOUDFLARE_LOG" >/dev/null 2>&1; then
    URL="$(grep -Eo 'https://[-a-zA-Z0-9]+\.trycloudflare\.com' "$CLOUDFLARE_LOG" | head -n1)"
    echo "[OK] Public frontend URL: $URL"
    break
  fi
  sleep 0.5
done

echo "[OK] Local frontend: http://127.0.0.1:${FRONTEND_PORT}"
echo "[OK] Local backend : http://127.0.0.1:${BACKEND_PORT}/api/health"

wait "$BACKEND_PID" "$FRONTEND_PID" "$CLOUDFLARED_PID"
