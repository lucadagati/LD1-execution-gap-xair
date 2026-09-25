#!/usr/bin/env bash
# All distributed campaigns behind the paper (root; ~2.5 h):
#   distributed/                  campaign 1, every suite
#   distributed/campaigns/c2..c5  independent repetitions (seed offsets 1..4) of the
#                                 timing-sensitive suites (E10 variants, E10-deadline, E16, E16-trace)
#   distributed/sensitivity/      jitter (2 ± 1 ms), shared CPUs, phase-locked E16-trace
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REP="e10_boundary e10_atomic e10_natural e10_natural_atomic e10_deadline e16 e16_trace"
SENS="e10_natural e10_natural_atomic e16"
rm -rf "$here/../experiments/results/distributed"
OUT_SUB=distributed "$here/run_distributed.sh" 0.5ms 0.1ms
for k in 2 3 4 5; do
  SUITES="$REP" SEED_OFFSET=$((k - 1)) OUT_SUB="distributed/campaigns/c$k" "$here/run_distributed.sh" 0.5ms 0.1ms
done
SUITES="$SENS" OUT_SUB=distributed/sensitivity/jitter2ms "$here/run_distributed.sh" 2ms 1ms
SUITES="$SENS" COMPOSE_EXTRA=compose.shared-cpu.yml OUT_SUB=distributed/sensitivity/shared_cpu "$here/run_distributed.sh" 0.5ms 0.1ms
SUITES="e16_trace" TRACE_PHASE=fixed OUT_SUB=distributed/sensitivity/phase_locked "$here/run_distributed.sh" 0.5ms 0.1ms
echo "All distributed campaigns complete"
