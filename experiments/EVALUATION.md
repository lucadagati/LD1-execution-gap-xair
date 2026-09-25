# Evaluation methodology

All suites drive the actuator gateway (`POST /intent?mode=<policy>`) or the XAIR
API over HTTP. Shared helpers, endpoints, Wilson intervals, and the single
nearest-rank percentile definition live in `common.py`.

## Primary endpoint

**Gateway release** (`gateway_released` in every gateway response): the intent
crossed the gateway into middleware. It does not depend on ROS 2 being
installed. `ros_published` (ROS publish call succeeded) and `ros_observed`
(independent ROS subscriber saw the message) are recorded as corroboration only.

- **SER** — releases / drift trials (every drift trial is obsolete at `t_v`).
- **CRR** — contextual revokes / drift trials.
- **FPR** — valid intents *not* released / valid intents.

## Policies (adapter modes)

| Mode | Context read | Check |
|------|--------------|-------|
| `direct` | — | none (failure floor) |
| `naive` | — | freshness/deadline only (failure floor) |
| `local` | refresh cache from XAIR, then cache | temporal + predicates |
| `local_stale` | cache, never refreshed | temporal + predicates |
| `local_push` | cache, refreshed iff `push_notified=true` | temporal + predicates |
| `local_authoritative` | shared snapshot, re-read at `t_p` | + version recheck |
| `xair` | XAIR validates at `t_v`; gateway re-reads at `t_p` | + version recheck, publication report |
| `xair_atomic` | XAIR validates at `t_v`; release is an atomic check-and-commit in XAIR | read-set version compared in the same store transaction that orders context updates (`POST /v1/intents/{id}/actuate`) |

All contextual modes use `xair.core` temporal validator and predicate evaluator.

## Gate semantics

Instants: decision t_d, validation t_v, gate recheck t_g, middleware release t_r.
The gate (xair and local_authoritative modes) compares a version at t_g with
the one recorded at t_v. `version_scope=readset` (default) uses the latest
value change on the paths the intent's predicates read (per-path versions in
the context store, `xair/core/versioning.py`); `version_scope=global` uses
the snapshot version advanced by every accepted update. The interval
(t_g, t_r] is not protected and is measured by E10 (`recheck_to_publish_ms`).

## Suites

| Suite | Script | Protocol notes |
|-------|--------|----------------|
| E0 | `run_e0_lifecycle.py` | 10 in-process lifecycle/contract regressions |
| E1 | `run_e1_baselines.py` | intent built (t_d), 200 ms pause, PAUSED written via gateway, submit; freshness 1000 ms |
| E1c | `run_e1_fpr.py` | valid-intent control, local + xair |
| E3 | `run_e3_http_stack.py` | AI + XR intents, same target, one batch; deterministic tie-break |
| E4 | `run_e4_http_load.py` | 10 000 sequential intents to XAIR core, distinct targets |
| E6 | `run_e6_network.py` | tc netem on `lo`; drifted + valid controls, reasons logged |
| E8-Gazebo | `run_e8_gazebo_cell.py` | ROS message witness + joint-motion witness, one file per campaign |
| E9 | `run_e9_consistency_sweep.py` | remote write to XAIR only; δ matters only for `local_push` (threshold L = 50 ms) |
| E10 | `run_e10_toctou.py` | injected write at an offset after validation, 50 ms induced gate delay; each trial classified before t_g (`before_recheck`) / in (t_g, t_r] (`concurrent`) / after t_r (`after_release`) |
| E11 | `run_e11_stratified.py` | mixed labels, 3 seeds; cost measured on valid trials only |
| E12 | `run_e12_scaling.py` | closed loop, persistent worker pool, retries counted |
| E13 | `run_e13_faults.py` | 5 malformed templates, false precondition, duplicates (release-boundary), clock skew |
| E14 | `run_e14_variants.py` | E1 pattern for RESUME / STOP / GRASP / SET_SPEED |
| E15 | `run_e15_opcua_hil.py` | line-state write through a local OPC UA server |
| E16 | `run_e16_context_churn.py` | valid intents under background writers at 0–500 Hz (unrelated field / same-value rewrite); `version_scope=global` vs `readset` |
| E16-trace | `run_e16_trace_churn.py` | UCI hydraulic test-rig recording (17 sensors, 1–100 Hz) replayed without interruption as OPC UA-style notifications at 1000/100/10 ms, 600 intents per interval interleaved at random and spread over the 300 s replay with a random phase; discrete vs continuous (100 Hz pressure) intents; scopes global / readset / predicate |

## Data provenance

`data/execution-gap/` holds every per-trial file behind the paper, all from one
host (2026-09-23), produced with the code in this tree:

- HTTP suites: `scripts/run_paper_campaign.sh`
- E6: `scripts/run_e6_netns.sh 30 10 500` and `... 30 10 10000 e6_network_fresh10s.csv`
  (stack inside a network namespace; netem never touches the host loopback)
- E8-Gazebo: `scripts/run_e8_docker.sh 30 <tag>` (image `docker/ros-jazzy`), campaigns 1 and 2;
  `experiments/make_paper_tables.py` excludes from the witness figures any campaign whose ROS
  subscriber stopped counting (none in the frozen data)
- E16: part of `scripts/run_paper_campaign.sh`
- Manuscript numbers and supplementary tables: `experiments/make_paper_tables.py`
  (called by `scripts/sync_paper_outputs.sh`)
- E15: `experiments/run_e15_opcua_hil.py --runs 30` (needs `asyncua`, in the `dev` extra)
- `pinned/`: E4 (three runs) and E12 on reserved cores with a dedicated Redis (`scripts/run_pinned_perf.sh`)
- `distributed/`: E1, E1c, E9, E10 (optimistic, atomic, natural window), E12, E16, E16-trace on
  the five-node testbed (`scripts/run_distributed.sh`, compose file `docker/distributed/compose.yml`)
- E16-trace data: UCI "Condition monitoring of hydraulic systems" (DOI 10.24432/C5CW21, CC BY 4.0),
  downloaded on first use into `experiments/.cache/` and verified by SHA-256 (`experiments/uci_hydraulic.py`)

## Known limitations

See the paper's threats-to-validity paragraph. In short: most suites are
deterministic by construction; E10's induced delay makes the window
observable but its offsets are artificial; the residual unprotected interval is
version-read → release call (`recheck_to_publish_ms`).
