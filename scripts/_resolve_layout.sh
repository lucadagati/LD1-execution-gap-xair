#!/usr/bin/env bash
# Resolve repository paths and service endpoints shared by every script.
# Sets: REPO_ROOT, SCRIPTS, PY, XAIR_PORT, ADAPTER_HTTP_PORT, ADAPTER_WS_PORT,
#       XAIR_URL, ADAPTER_URL, XAIR_RESULTS_DIR, RUN_DIR
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$_here/.." && pwd)"
SCRIPTS="$_here"
unset _here

if [ ! -d "$REPO_ROOT/xair/core" ] || [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
  echo "ERROR: $REPO_ROOT does not look like the XAIR repository (xair/core, pyproject.toml)" >&2
  exit 1
fi

XAIR_PORT="${XAIR_PORT:-8080}"
ADAPTER_HTTP_PORT="${ADAPTER_HTTP_PORT:-9092}"
ADAPTER_WS_PORT="${ADAPTER_WS_PORT:-9091}"
XAIR_URL="${XAIR_URL:-http://127.0.0.1:$XAIR_PORT}"
ADAPTER_URL="${ADAPTER_URL:-http://127.0.0.1:$ADAPTER_HTTP_PORT}"
XAIR_RESULTS_DIR="${XAIR_RESULTS_DIR:-$REPO_ROOT/experiments/results}"
RUN_DIR="${RUN_DIR:-$REPO_ROOT/.run}"

# Project interpreter: the repository venv if present, else python3 on PATH.
# PY may be preset (e.g. the ROS container uses the system python that has rclpy).
if [ -z "${PY:-}" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python" ] && "$REPO_ROOT/.venv/bin/python" -c "" 2>/dev/null; then
    PY="$REPO_ROOT/.venv/bin/python"
  else
    PY="$(command -v python3)"
  fi
fi

export REPO_ROOT SCRIPTS PY XAIR_PORT ADAPTER_HTTP_PORT ADAPTER_WS_PORT XAIR_URL ADAPTER_URL XAIR_RESULTS_DIR RUN_DIR
