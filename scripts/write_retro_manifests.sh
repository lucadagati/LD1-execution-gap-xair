#!/usr/bin/env bash
# Write MANIFEST.json for every frozen campaign directory produced before manifests existed
# (data/execution-gap/). Commands and seeds are those of the scripts that produced each
# directory, as documented in data/execution-gap/README.md; what was not recorded at run time
# (commit, image digests) is marked as such in each manifest. Safe to rerun: it only
# rewrites MANIFEST.json files.
set -euo pipefail
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_resolve_layout.sh"
D="$REPO_ROOT/data/execution-gap"
M=("$PY" "$REPO_ROOT/scripts/make_manifest.py")
SEEDS_DIST='per suite in scripts/run_distributed.sh: e1 42+S, e10 boundary/atomic 43+S, e10 natural 44+S, e10-deadline 10+S, e10-aba 11+S, e21 21+S, e21-aba 31+S, e16 42+S, e16-trace 16+S, e20 20+S, e18 18+S (S = SEED_OFFSET in environment.txt)'

"${M[@]}" --retro "$D" --flat --command "./scripts/run_paper_campaign.sh" \
  --command ".venv/bin/python experiments/run_e15_opcua_hil.py --runs 30" --command "sudo ./scripts/run_e6_netns.sh 30 10 500" \
  --command "./scripts/run_e8_docker.sh 30 <1|2>" --command "./scripts/sync_paper_outputs.sh" \
  --seed "per step in scripts/run_paper_campaign.sh (e1 42, e10 42/43, e11 42/7/123)" \
  --note "single host (S), 2026-09-23 (environment.txt); e0_lifecycle.json regenerated 2026-10-06 with the v1.7 lifecycle (14 cases; the v1.6 file had 10)"
"${M[@]}" --retro "$D/pinned" --command "./scripts/run_pinned_perf.sh" --note "reserved cores (R)"
"${M[@]}" --retro "$D/distributed" --flat --command "OUT_SUB=distributed sudo ./scripts/run_distributed.sh 0.5ms 0.1ms" \
  --command "(cd experiments && python run_e18b_durability.py --n 1000 --reps 5)" --seed "$SEEDS_DIST; S=0" \
  --note "campaign 1 of scripts/run_distributed_campaigns.sh; E20 here is the v1.6 rotation design (run_e20_mes.py --design rotation --seed 20)"
for k in 2 3 4 5; do
  "${M[@]}" --retro "$D/distributed/campaigns/c$k" \
    --command "SUITES=\"e10_boundary e10_atomic e10_natural e10_natural_atomic e10_deadline e10_aba e21 e21_aba e16 e16_trace\" SEED_OFFSET=$((k - 1)) OUT_SUB=distributed/campaigns/c$k sudo ./scripts/run_distributed.sh 0.5ms 0.1ms" \
    --seed "$SEEDS_DIST; S=$((k - 1))"
done
"${M[@]}" --retro "$D/distributed/sensitivity/jitter2ms" --command "SUITES=\"e10_natural e10_natural_atomic e16\" OUT_SUB=distributed/sensitivity/jitter2ms sudo ./scripts/run_distributed.sh 2ms 1ms" --seed "$SEEDS_DIST; S=0"
"${M[@]}" --retro "$D/distributed/sensitivity/shared_cpu" --command "SUITES=\"e10_natural e10_natural_atomic e16\" COMPOSE_EXTRA=compose.shared-cpu.yml OUT_SUB=distributed/sensitivity/shared_cpu sudo ./scripts/run_distributed.sh 0.5ms 0.1ms" --seed "$SEEDS_DIST; S=0"
"${M[@]}" --retro "$D/distributed/sensitivity/phase_locked" --command "SUITES=e16_trace TRACE_PHASE=fixed OUT_SUB=distributed/sensitivity/phase_locked sudo ./scripts/run_distributed.sh 0.5ms 0.1ms" --seed "$SEEDS_DIST; S=0"
"${M[@]}" --retro "$D/physical" --command "PHYS_HOST=user@host ./scripts/run_physical.sh" --note "wide-area node (P); one environment file per suite group"
"${M[@]}" --retro "$D/lan" --command "CORE_HOST=.. CORE_IP=.. EDGE_HOST=.. EDGE_IP=.. ./scripts/run_lan.sh" --note "Wi-Fi LAN node (L)"
"${M[@]}" --retro "$D/scaling" --command "(cd experiments && python run_e23_truth_scaling.py)" --seed 23 --note "E23; dedicated Redis container redis:7-alpine"
"${M[@]}" --retro "$D/openplc" --command "./scripts/run_openplc.sh" --note "E24; program docker/openplc/cell_ctl.st as of v1.6 (registers 0-8; register 9, the conveyor, was added in v1.7 without changing decisions or result codes)"
"${M[@]}" --retro "$D/embedded" --flat --command "BOARD=<rpi4|vf2> ./scripts/run_embedded.sh" --note "board-independent environment record of E25"
for b in rpi4 vf2; do
  "${M[@]}" --retro "$D/embedded/$b" --command "BOARD=$b ./scripts/run_embedded.sh" --note "E25; pinning recorded in environment.txt"
done
