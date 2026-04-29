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

TAIL_BE=""
TAIL_FE=""

cleanup() {
  local code=$?
  # Disarm the trap immediately so a second Ctrl+C while we're inside
  # cleanup() doesn't trigger a re-entrant cleanup() — that's what
  # caused the infamous "shutting down..." stack of N copies before:
  # each ^C re-fired the trap while ``wait`` was still blocked on a
  # uvicorn process that itself was hung on "Waiting for background
  # tasks to complete." With the trap removed, a second ^C just
  # raises SIGINT against bash's default handler and the script exits
  # immediately.
  trap - INT TERM EXIT
  echo
  info "shutting down..."

  # Kill the log-tail subshells right away — they only stream files,
  # nothing graceful to do.
  [[ -n "$TAIL_BE" ]] && kill "$TAIL_BE" 2>/dev/null || true
  [[ -n "$TAIL_FE" ]] && kill "$TAIL_FE" 2>/dev/null || true

  local pids=()
  [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null && pids+=("$FRONTEND_PID")
  [[ -n "$BACKEND_PID" ]]  && kill -0 "$BACKEND_PID"  2>/dev/null && pids+=("$BACKEND_PID")

  if [[ ${#pids[@]} -gt 0 ]]; then
    # SIGTERM the process groups so vite / uvicorn workers / tail
    # subshells all die with their starter.
    for pid in "${pids[@]}"; do
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done

    # Wait up to 3s for graceful exit. uvicorn's "Waiting for
    # background tasks to complete" path can hang indefinitely
    # whenever an asyncpg pool / httpx client / MCP transport task
    # outlives the lifespan hook, so we cap it ourselves rather than
    # trusting --timeout-graceful-shutdown (which only times out HTTP
    # connections, not the lifespan task wait).
    local waited=0
    while (( waited < 30 )); do  # 30 * 100ms = 3s
      local alive=0
      for pid in "${pids[@]}"; do
        kill -0 "$pid" 2>/dev/null && alive=1
      done
      (( alive == 0 )) && break
      sleep 0.1
      waited=$((waited + 1))
    done

    # Anything still alive after the grace period: SIGKILL.
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        warn "force-killing pid=$pid (graceful shutdown timed out)"
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
      fi
    done
  fi

  info "containers still up (run 'docker compose down' to stop postgres+redis)"
  exit "$code"
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
  # ``--timeout-graceful-shutdown 5`` caps how long uvicorn will wait
  # for in-flight requests + background tasks to finish on a reload.
  # Combined with the lifespan hook that actively cancels chatflow
  # runtimes on shutdown, this means a backend code edit reloads in
  # 5 seconds max even if a long qwen36 ``submit_turn`` is mid-flight.
  exec uvicorn agentloom.main:app --reload --host 0.0.0.0 --port 8000 \
    --timeout-graceful-shutdown 5
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
