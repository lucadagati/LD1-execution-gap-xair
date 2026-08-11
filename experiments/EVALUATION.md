# Evaluation methodology (IEEE TII)

## Client path: HTTP protocol replay (Path B)

Primary evaluation uses **HTTP protocol replay** via `run_e1_baselines.py` — identical AIS JSON to the Unity `DefectEventEmitter` client. Unity Editor Play Mode is supported as optional replication (see `AdaptiX-Quest/README.md`).

**Do not claim Unity Editor evaluation unless `ManufacturingTestOrchestrator` batch was executed on Mac and JSON exported with `_unity` suffix.**

## Symmetric baselines

All baselines use the same HTTP path (`POST :9092/intent?mode=`):

| Mode | Behavior |
|------|----------|
| `xair` | Full temporal + contextual validation via XAIR |
| `naive` | Freshness window only; ignores context |
| `direct` | No validation; always publishes to ROS |

## Scenario E1b

Stale **RESUME** when line is **PAUSED** and gripper **CLOSED** — precondition `line.state == 'RUN'` fails at $t_e$.

Protocol order (aligned Unity + Python): RUN → delay → context change → submit intent.

## Metrics

- **SER** = stale_executed / attempted (ros_published when context invalid)
- **SER$_{\mathrm{known}}$** (E8) = stale_executed / completed runs (excludes UNKNOWN transport failures)
- **POA** = correct_revokes / obsolete_intents (excludes UNKNOWN)
- **FPR** = wrongful_revokes / valid_intents (E1c)
- **CV** = conflict violations (E3)
- **UR** = unknown_rate

## Reproduce

```bash
./scripts/start_full_stack.sh
./scripts/verify_e2e.sh
cd XAIR_Runtime
.venv/bin/python experiments/run_e1_baselines.py --runs 30 --seed 42
.venv/bin/python experiments/run_e1_fpr.py --runs 30
.venv/bin/python experiments/run_e3_http_stack.py --runs 30
.venv/bin/python experiments/run_e4_http_load.py --intents 1000
.venv/bin/python experiments/aggregate_experiment_results.py
.venv/bin/python experiments/plot_results.py
```
