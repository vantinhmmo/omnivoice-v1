#!/usr/bin/env bash
set -euo pipefail

# OmniVoice one-command setup + run + Cloudflare quick tunnel
# Usage:
#   bash scripts/setup_and_run.sh
# Optional env:
#   OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=2 BACKEND_PORT=8000 FRONTEND_PORT=5173 bash scripts/setup_and_run.sh
#   ENABLE_CLOUDFLARE_TUNNEL=1 bash scripts/setup_and_run.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
OMNIVOICE_DTYPE="${OMNIVOICE_DTYPE:-auto}"
OMNIVOICE_MAX_CONCURRENT_LONG_JOBS="${OMNIVOICE_MAX_CONCURRENT_LONG_JOBS:-2}"
ENABLE_CLOUDFLARE_TUNNEL="${ENABLE_CLOUDFLARE_TUNNEL:-1}"

# Auto device fallback
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

echo "[INFO] ROOT_DIR=$ROOT_DIR"
echo "[INFO] OMNIVOICE_DEVICE=$OMNIVOICE_DEVICE"
echo "[INFO] OMNIVOICE_DTYPE=$OMNIVOICE_DTYPE"
echo "[INFO] OMNIVOICE_MAX_CONCURRENT_LONG_JOBS=$OMNIVOICE_MAX_CONCURRENT_LONG_JOBS"

echo "[STEP] Checking required tools..."
command -v npm >/dev/null 2>&1 || { echo "[ERROR] npm not found (install Node.js first)"; exit 1; }

# Python resolver: prefer python3, fallback python/py
PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
elif command -v py >/dev/null 2>&1; then
  PYTHON_CMD="py"
else
  echo "[ERROR] No Python command found (python3/python/py)"
  exit 1
fi

echo "[INFO] Using Python command: $PYTHON_CMD"

if [[ ! -f "requirements.txt" ]]; then
  echo "[ERROR] requirements.txt not found in $ROOT_DIR"
  echo "[HINT] Run this script from project root or keep it under scripts/ as-is"
  exit 1
fi

PYTHON_BIN=""
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
else
  echo "[STEP] Creating virtual env .venv ..."
  "$PYTHON_CMD" -m venv .venv
  PYTHON_BIN=".venv/bin/python"
fi

echo "[STEP] Installing backend dependencies..."
"$PYTHON_BIN" -m pip install -U pip setuptools wheel
"$PYTHON_BIN" -m pip install -r requirements.txt

echo "[STEP] Installing frontend dependencies..."
pushd frontend >/dev/null
if [[ -f package-lock.json ]]; then
  npm ci
else
  npm install
fi
popd >/dev/null

echo "[STEP] Starting backend on ${BACKEND_HOST}:${BACKEND_PORT} ..."
"$PYTHON_BIN" -m uvicorn server.api:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" &
BACKEND_PID=$!

echo "[STEP] Starting frontend on ${FRONTEND_HOST}:${FRONTEND_PORT} ..."
pushd frontend >/dev/null
npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
FRONTEND_PID=$!
popd >/dev/null

CLOUDFLARED_PID=""
CLOUDFLARE_LOG="/tmp/omnivoice_cloudflared.log"

start_cloudflare_quick_tunnel() {
  if [[ "$ENABLE_CLOUDFLARE_TUNNEL" != "1" ]]; then
    echo "[INFO] Cloudflare tunnel disabled (ENABLE_CLOUDFLARE_TUNNEL=$ENABLE_CLOUDFLARE_TUNNEL)"
    return
  fi

  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "[INFO] cloudflared not found. Installing quick tunnel client..."
    if command -v curl >/dev/null 2>&1; then
      tmp_dir="$(mktemp -d)"
      arch="$(uname -m)"
      case "$arch" in
        x86_64|amd64) cf_arch="amd64" ;;
        aarch64|arm64) cf_arch="arm64" ;;
        *)
          echo "[WARN] Unsupported arch for auto-install cloudflared: $arch"
          return
          ;;
      esac
      curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}" -o "$tmp_dir/cloudflared"
      chmod +x "$tmp_dir/cloudflared"
      sudo mv "$tmp_dir/cloudflared" /usr/local/bin/cloudflared 2>/dev/null || mv "$tmp_dir/cloudflared" "$ROOT_DIR/cloudflared"
      rm -rf "$tmp_dir"
      if [[ -x "$ROOT_DIR/cloudflared" ]]; then
        export PATH="$ROOT_DIR:$PATH"
      fi
    else
      echo "[WARN] curl not found, skip auto-install cloudflared"
      return
    fi
  fi

  echo "[STEP] Starting Cloudflare quick tunnel for frontend ${FRONTEND_HOST}:${FRONTEND_PORT} ..."
  rm -f "$CLOUDFLARE_LOG"
  cloudflared tunnel --url "http://${FRONTEND_HOST}:${FRONTEND_PORT}" > "$CLOUDFLARE_LOG" 2>&1 &
  CLOUDFLARED_PID=$!

  # Wait up to 20s for public URL
  for _ in $(seq 1 40); do
    if grep -Eo 'https://[-a-zA-Z0-9]+\.trycloudflare\.com' "$CLOUDFLARE_LOG" >/dev/null 2>&1; then
      url="$(grep -Eo 'https://[-a-zA-Z0-9]+\.trycloudflare\.com' "$CLOUDFLARE_LOG" | head -n1)"
      echo "[OK] Cloudflare public frontend URL: $url"
      return
    fi
    sleep 0.5
  done

  echo "[WARN] Could not parse Cloudflare URL yet. Check log: $CLOUDFLARE_LOG"
}

cleanup() {
  echo ""
  echo "[INFO] Stopping services..."
  if [[ -n "$CLOUDFLARED_PID" ]]; then
    kill "$CLOUDFLARED_PID" >/dev/null 2>&1 || true
    wait "$CLOUDFLARED_PID" >/dev/null 2>&1 || true
  fi
  kill "$FRONTEND_PID" >/dev/null 2>&1 || true
  kill "$BACKEND_PID" >/dev/null 2>&1 || true
  wait "$FRONTEND_PID" >/dev/null 2>&1 || true
  wait "$BACKEND_PID" >/dev/null 2>&1 || true
  echo "[INFO] Done."
}

trap cleanup INT TERM EXIT

echo "[OK] Backend PID=$BACKEND_PID | Frontend PID=$FRONTEND_PID"
echo "[OK] Local frontend: http://127.0.0.1:${FRONTEND_PORT}"
echo "[OK] Local health API: http://127.0.0.1:${BACKEND_PORT}/api/health"

start_cloudflare_quick_tunnel

wait "$BACKEND_PID" "$FRONTEND_PID"
