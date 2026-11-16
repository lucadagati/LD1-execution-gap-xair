#!/usr/bin/env bash
# E6 inside a dedicated network namespace, so tc netem on its loopback cannot
# degrade other services on the host. Requires root (ip netns, tc).
# Usage: sudo ./scripts/run_e6_netns.sh [drifted-runs] [valid-controls] [freshness-ms] [out-csv-name]
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
NS="${E6_NETNS:-xair-e6}"

ip netns list | grep -qw "$NS" || ip netns add "$NS"
ip netns exec "$NS" ip link set lo up
cleanup() {
  ip netns exec "$NS" tc qdisc del dev lo root 2>/dev/null || true
  ip netns pids "$NS" 2>/dev/null | xargs -r kill 2>/dev/null || true
  sleep 1
  ip netns del "$NS" 2>/dev/null || true
}
trap cleanup EXIT

# Inside the namespace the default ports are free and Redis is not reachable:
# use the in-memory store (single XAIR process).
ip netns exec "$NS" env RUN_DIR="$REPO_ROOT/.run/netns-$NS" REDIS_URL= XAIR_PORT=8080 ADAPTER_HTTP_PORT=9092 ADAPTER_WS_PORT=9091 \
  XAIR_URL=http://127.0.0.1:8080 ADAPTER_URL=http://127.0.0.1:9092 XAIR_RESULTS_DIR="$XAIR_RESULTS_DIR" \
  bash -c "'$SCRIPTS/start_full_stack.sh' && '$PY' '$REPO_ROOT/experiments/run_e6_network.py' --use-netem --runs '${1:-30}' --controls '${2:-10}' --freshness-ms '${3:-500}' --out '$XAIR_RESULTS_DIR/${4:-e6_network.csv}'"
