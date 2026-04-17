#!/usr/bin/env bash
# Agentloom one-terminal dev launcher.
# Starts postgres+redis (docker), backend (uvicorn), frontend (vite) in one window.
# Ctrl+C stops everything cleanly.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/.dev-logs"
mkdir -p "$LOG_DIR"

BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"
BACKEND_PID=""
FRONTEND_PID=""

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
# shellcheck source=/dev/null
source "$CONDA_BASE/etc/profile.d/conda.sh"

color() { printf '\033[%sm%s\033[0m' "$1" "$2"; }
info()  { printf '%s %s\n' "$(color '36' '[dev]')" "$*"; }
warn()  { printf '%s %s\n' "$(color '33' '[dev]')" "$*" >&2; }
err()   { printf '%s %s\n' "$(color '31' '[dev]')" "$*" >&2; }

cleanup() {
  local code=$?
  echo
  info "shutting down..."
  if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill -- -"$FRONTEND_PID" 2>/dev/null || kill "$FRONTEND_PID" 2>/dev/null || true
  fi
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill -- -"$BACKEND_PID" 2>/dev/null || kill "$BACKEND_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  info "containers still up (run 'docker compose down' to stop postgres+redis)"
  exit $code
}
trap cleanup INT TERM EXIT

cd "$ROOT"

info "starting postgres + redis via docker compose..."
docker compose up -d postgres redis >/dev/null

info "waiting for postgres healthy..."
for i in {1..30}; do
  status=$(docker inspect -f '{{.State.Health.Status}}' agentloom-postgres 2>/dev/null || echo "missing")
  [[ "$status" == "healthy" ]] && break
  sleep 1
done
[[ "$status" == "healthy" ]] || { err "postgres not healthy"; exit 1; }

info "activating conda env 'agentloom'..."
conda activate agentloom

info "running alembic upgrade head..."
(cd backend && alembic upgrade head) || warn "alembic failed — continuing anyway"

info "launching backend (uvicorn :8000) — log: $BACKEND_LOG"
(
  cd backend
  exec uvicorn agentloom.main:app --reload --host 0.0.0.0 --port 8000
) > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

info "launching frontend (vite :5173) — log: $FRONTEND_LOG"
(
  cd frontend
  exec npm run dev
) > "$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

sleep 1
info "backend pid=$BACKEND_PID  frontend pid=$FRONTEND_PID"
info "streaming logs — Ctrl+C stops both services"
echo

# Prefix each log line with a tag so the two streams are distinguishable.
tail -n 0 -F "$BACKEND_LOG"  | sed -u "s/^/$(color '35' '[be]') /" &
TAIL_BE=$!
tail -n 0 -F "$FRONTEND_LOG" | sed -u "s/^/$(color '32' '[fe]') /" &
TAIL_FE=$!

# Exit if either main service dies.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 2
done

kill "$TAIL_BE" "$TAIL_FE" 2>/dev/null || true
err "one of the services exited — see logs in $LOG_DIR"
exit 1
