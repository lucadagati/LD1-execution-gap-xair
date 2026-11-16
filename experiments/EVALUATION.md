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
- **FPR** — valid intents *not* released / valid intents.

## Policies (adapter modes)

Paper names: `naive` = freshness-only floor, `local` = coherent local guard, `local_stale` = stale cache, `local_push` = push policy, `local_authoritative` = read-through, `version_scope=predicate` = predicate-only gate.

| Mode | Context read | Check |
|------|--------------|-------|
| `direct` | — | none (failure floor) |
| `naive` | — | freshness/deadline only (failure floor) |
| `local` | refresh cache from XAIR, then cache | temporal + predicates |
| `local_stale` | cache, never refreshed | temporal + predicates |
| `local_push` | cache, refreshed iff `push_notified=true` | temporal + predicates |
| `local_authoritative` | shared snapshot, re-read at `t_g` | + version recheck |
| `xair` | XAIR validates at `t_v`; gateway re-reads at `t_g` | + version recheck, publication report |
| `opa` | Open Policy Agent decides the same predicates at arrival and before release (context replicated by XAIR write-through) | predicates only, no versions |
| `xair_atomic` | XAIR validates at `t_v`; atomic authorization commit `t_c` in XAIR, then release `t_m` | Lua script on the store: read-set version, age vs deadline on the store clock, idempotent append to the actuation log (`POST /v1/intents/{id}/commit`) |

All contextual modes use `xair.core` temporal validator and predicate evaluator.

## Gate semantics

Instants: decision t_d, validation t_v, gate store read t_g, authorization
commit t_c (atomic mode), middleware release t_m, actuator effect t_a.
The gate (xair and local_authoritative modes) compares a version at t_g with
the one recorded at t_v. `version_scope=readset` (default) uses the latest
value change on the paths the intent's predicates read (per-path versions in
the context store, `xair/core/versioning.py`); `version_scope=truth` the
predicate-truth version, which advances only when a registered predicate
changes truth value; `version_scope=global` the snapshot version advanced by
every accepted update; `version_scope=predicate` re-evaluates the predicates
without a version. The interval (t_g, t_m] of the optimistic mode, and
(t_c, t_m] of the atomic mode, are not protected; E10 measures them
(`recheck_to_publish_ms`). A conditional command at the controller (E21, E24)
checks the controller's own version in the scan that produces the effect.

## Suites

| Suite | Script | Protocol notes |
|-------|--------|----------------|
| E0 | `run_e0_lifecycle.py` | 14 in-process lifecycle/contract regressions (authorization, commit, release, effect as distinct states) |
| E1 | `run_e1_baselines.py` | intent built (t_d), 200 ms pause, PAUSED written via gateway, submit; freshness 1000 ms |
| E1c | `run_e1_fpr.py` | valid-intent control, local + xair |
| E3 | `run_e3_http_stack.py` | AI + XR intents, same target, one batch; deterministic tie-break |
| E4 | `run_e4_http_load.py` | 10 000 sequential intents to XAIR core, distinct targets |
| E6 | `run_e6_network.py` | tc netem on `lo`; drifted + valid controls, reasons logged |
| E8-Gazebo | `run_e8_gazebo_cell.py` | ROS message witness + joint-motion witness, one file per campaign |
| E9 | `run_e9_consistency_sweep.py` | remote write to XAIR only; δ matters only for `local_push` (threshold L = 50 ms) |
| E10 | `run_e10_toctou.py` | injected write at an offset after validation, 50 ms induced gate delay or none (natural window), optimistic and atomic release; each write ordered against the check by the store version it created and placed against t_m by shared-clock bounds (`version_order`, `position_vs_middleware`); the gateway-clock class before t_g / in (t_g, t_m] / after t_m is kept for comparison |
| E11 | `run_e11_stratified.py` | mixed labels, 3 seeds; cost measured on valid trials only |
| E12 | `run_e12_scaling.py` | closed loop, persistent worker pool, retries counted |
| E13 | `run_e13_faults.py` | 5 malformed templates, false precondition, duplicates (release-boundary), clock skew |
| E14 | `run_e14_variants.py` | E1 pattern for RESUME / STOP / GRASP / SET_SPEED |
| E15 | `run_e15_opcua_hil.py` | line-state write through a local OPC UA server (software-in-the-loop; the `hil` file suffix is historical) |
| E16 | `run_e16_context_churn.py` | valid intents under background writers at 0–500 Hz (unrelated field / same-value rewrite); scopes global / readset / truth and OPA |
| E10-deadline | `run_e10_deadline.py` | deadline 100 ms, induced gate delay swept 75–105 ms in 0.5 ms steps; age at the check (gateway clock / store clock) and at `t_m`; late releases counted |
| E10-ABA | `run_e10_aba.py` | line paused and resumed while the gate waits 50 ms; gates: XAIR readset / truth / predicate-only (optimistic), readset / truth (atomic), OPA with predicates only and with its read-set rule (`opa/opa_readset`); each flip trial records the store versions of validation, pause, resume, and check (`protocol` = valid / flip_outside_window), and `--until-valid` repeats flips until every gate has `--runs` valid trials (used on the physical nodes) |
| E20 | `run_e20_mes.py`, `mes_trace.py` | 4TU job-shop log (DOI 10.4121/uuid:68726926-5ac5-4fab-b873-ee76ea412399): offline drift prevalence for decision latencies 1 s–1 h (exact over the timeline; one-minute timestamps), plus `e20_mes_sensitivity.csv`: three decision models (uniform over idle time, idle intervals ≤ 8 h, one decision at idle start) × three invalidation proxies (leaves idle, next step for a different part, next step a breakdown), with per-resource spread; live replay: 30 days, START_JOB on idle machines after 30 or 240 plant minutes, gates direct / global / readset / truth / atomic-truth, ground truth = snapshot as written. v1.6 data (`distributed/e20_mes_replay.csv`): rotation design, 8640× faster, 1000 decisions per latency, each through one gate (200 per gate). v1.7 data (`distributed/e20_paired/`): paired design, 4320× faster, 2000 decisions per latency, each through all five gates with the same decision time, machine and precondition |
| E21 | `run_e21_cell.py`, `scripts/cell_controller.py` | OPC UA cell controller owns the line state (10 ms scan) and mirrors it to XAIR; operator pause at the controller; unconditional command, controller-local interlock (`plc_predicate`, executes only on RUN), and version-conditional command, optimistic and atomic. With `--aba-gap-ms` (E21-ABA, `e21_cell_aba.csv`) the pause is followed by a resume; `history_violation` = executed on RUN at a controller version newer than the one the check observed (`witness_version`) |
| E18b | `run_e18b_durability.py` | host-side: Redis with AOF fsync always, committer and consumer SIGKILLed and restarted, Redis killed twice per run; SQLite-backed idempotent actuator |
| E17 | `run_e17_policy.py` | producer omits `line.state == 'RUN'`; drift and valid trials with and without server-side policy (`PUT /v1/policy`) |
| E18 | `run_e18_outbox_faults.py` | actuation-log fault model against Redis db 2: gateway crash after commit, duplicate commit, consumer crash after apply, consumer restarts, concurrent commits, post-commit invalidation |
| E16-trace | `run_e16_trace_churn.py` | UCI hydraulic test-rig recording (17 sensors, 1–100 Hz) replayed without interruption as OPC UA-style notifications at 1000/100/10 ms, 1200 intents per interval (twelve cells, n = 100) interleaved at random and spread over the 300 s replay with a random phase; discrete vs continuous (100 Hz pressure) intents; scopes global / readset / truth / predicate, OPA and OPA with its read-set rule (`opa_readset`) |
| E22 | `scripts/run_physical.sh`, `scripts/run_lan.sh` | gateway and cell controller on a separate machine, over a wide-area overlay (`physical/`) or on a Wi-Fi LAN with XAIR, Redis, and the producers on a Raspberry Pi 4 (`lan/`): E1, E1c, E10 natural window, E10-ABA, E16, E21, E21-ABA |
| E23 | `run_e23_truth_scaling.py` | 0–10 000 registered predicates, updates changing 0–100 % of their paths, document vs hash layout, retirement; dedicated Redis |
| E24 | `run_e21_cell.py --backend openplc`, `scripts/run_openplc.sh` | E21 protocol on an OpenPLC v3 runtime (Structured Text, 10 ms task, Modbus TCP); unconditional, interlock on the line state (`cmd_mode` 2), or version-conditional command (`cmd_mode` 1); no deadline check in the PLC |
| E25 | `scripts/run_embedded.sh` | XAIR on embedded boards (Raspberry Pi 4, VisionFive 2): Redis, XAIR, gateway, and harness each pinned to one core over loopback; E4 (3 × 3000), E11 (three seeds), E10 natural window (both modes), E16 (0–100 Hz) |
| E25b | `scripts/run_embedded_eq3.sh`, `experiments/analysis_eq3_empirical.py` | Eq. (4) check on the boards: E16 churn cells (global scope) at 20/50/100 Hz, 3 seeds × 400 trials per pattern; the analysis compares the measured revocation rate with the prediction on the median and on the empirical distribution of `validation_to_gate_ms`; `chrony_*.txt` hold the clock state before and after |
| E26 | `run_e26_controller_churn.py`, `scripts/cell_controller_multi.py` | valid conditional commands under churn of controller-owned state: three fast variables at 0/20/50/100 Hz each; version scope global (any controller change advances it) or line (only the line state); 400 per release path and cell, plus 100 pause–resume injections per path (history violations); `.scan.json` holds the scan duration (mean:p99:max, µs) |
| E27 | `run_e27_stop_resume.py` | RESUME with its gate/commit delayed 40 ms and a STOP of the same target 0–60 ms later, optimistic and atomic, with the stop fence off (`XAIR_STOP_FENCE=0`) and on; stale = RESUME released after the STOP (`t_middleware_wall_ms`); 330 injected + 100 valid controls per cell |
| E28 | `run_e28_truth_scale.py` | 10 / 1000 / 10000 registered predicates, paced updates through XAIR (20 and 100 Hz, 10% of the paths per update), truth-version intents (valid controls and pause–resume flips) in both modes, document vs hash vs per-root (`shard`) registry layout (`--layouts`, `--touch`; the per-root runs use `XAIR_STOP_FENCE=0`); `.writer.json` holds update latency and achieved rate |
| E29 | `run_e29_delay_degrade.py` | 300 trials each of DELAY (second intent on a busy target, executed after the holder is released) and DEGRADE (reduce-speed policy, second pass under the same id with speed factor 0.5), in-process |
| E27-skew | `run_e27_stop_resume.py --eps-ms --producer-offset-ms --after-offsets-ms` | E27 with the producer's clock offset by ±55 ms from XAIR's and the clock bound ε of XAIR at 0 or 60 ms; RESUME decided 0–120 ms after a STOP checks the cost of the margin |
| E10-deadline-eps | `run_e10_deadline.py --remote-offset-ms --producer-offset-ms`, `run_physical.sh`, `run_lan.sh` (`DL_MS`, `DL_DELAYS`, `PHYS_EPS`, `LAN_EPS`) | deadline experiment on the wide-area and LAN nodes with the measured clock offset of the gateway, an emulated producer offset, and ε |
| E21-gap / E21-sampling | `run_distributed.sh` (`ABA_GAPS`, `E21_ABA_OFFSETS`, `E21_OFFSETS`), `compose.sampling{50,200}.yml`, `cell_controller.py --sampling-ms` | pause durations of 3–100 ms and a controller that publishes its state once per sampling interval (queue of one) |
| E24-epoch | `run_plc_wrap.sh`, `run_plc_wrap.py` | 32768 version changes on OpenPLC: the epoch of `cell_ctl.st` keeps an old witness from matching the wrapped counter |
| Repeats | `CAMPAIGN=k` of `run_physical.sh`, `run_lan.sh`, `run_embedded.sh`, `run_openplc.sh` | two repeats of E22, E25, E24 and E26 with the stop fence off, as the original campaigns |
| E25-fence | `FENCE_COST=1 XAIR_STOP_FENCE=0/1 BOARD=... run_embedded.sh` | throughput of the current build without and with the stop fence |
| E25b-instrumented | `run_embedded_eq3.sh` (`EQ3_SUFFIX=_instr`), `run_e16_context_churn.py --writer poisson` | Eq. (4) check with the store reads of validation and gate reported and with a Poisson update writer |
| E25b-legacy | `XAIR_LEGACY_SEPARATE_READ=1 EQ3_SUFFIX=_legacy run_embedded_eq3.sh` | ablation of the first campaign's order (snapshot read, then predicates registered in a second round trip); same cells as E25b-instrumented; shows how much of the first campaign's Eq. (4) underestimate the order explains (Pi yes, VisionFive 2 no) |

## Data provenance

`data/execution-gap/` holds every per-trial file behind the paper (see its
README for the configuration of each subset), produced with the code in this tree:

- HTTP suites: `scripts/run_paper_campaign.sh`
- E6: `scripts/run_e6_netns.sh 30 10 500` and `... 30 10 10000 e6_network_fresh10s.csv`
  (stack inside a network namespace; netem never touches the host loopback)
- E8-Gazebo: `scripts/run_e8_docker.sh 30 <tag>` (image `docker/ros-jazzy`), campaigns 1 and 2;
  `experiments/make_paper_tables.py` excludes from the witness figures any campaign whose ROS
  subscriber stopped counting (none in the frozen data)
- E16: part of `scripts/run_paper_campaign.sh`
- Paper tables and number macros: `experiments/make_paper_tables.py`; detailed-results document
  (`docs/detailed-results/`): `scripts/build_detailed_results.sh` (both called by `scripts/sync_paper_outputs.sh`)
- E15: `experiments/run_e15_opcua_hil.py --runs 30` (needs `asyncua`, in the `dev` extra)
- `pinned/`: E4 (three runs) and E12 on reserved cores with a dedicated Redis (`scripts/run_pinned_perf.sh`)
- `distributed/`: E1, E1c, E9, E10 (optimistic, atomic, natural window), E10-deadline, E10-ABA, E12, E16, E16-trace, E17, E18, E20, E21, E21-ABA;
  `distributed/campaigns/c2..c5`: independent repetitions of the timing-sensitive suites;
  `distributed/sensitivity/`: 2 ± 1 ms jitter, shared CPUs, phase-locked E16-trace (`scripts/run_distributed_campaigns.sh`); on
  the container testbed (`scripts/run_distributed.sh`, compose file `docker/distributed/compose.yml`); E20, E21 and E21-ABA
  are part of the same campaigns
- `physical/`, `lan/`: E22 (`scripts/run_physical.sh`, `scripts/run_lan.sh`)
- `openplc/`: E24 (`scripts/run_openplc.sh`); `scaling/`: E23 (`experiments/run_e23_truth_scaling.py`)
- `embedded/rpi4`, `embedded/vf2`: E25 (`scripts/run_embedded.sh`)
- E16-trace data: UCI "Condition monitoring of hydraulic systems" (DOI 10.24432/C5CW21, CC BY 4.0),
  downloaded on first use into `experiments/.cache/` and verified by SHA-256 (`experiments/uci_hydraulic.py`)

## Known limitations

See the paper's threats-to-validity paragraph. In short: most suites are
deterministic by construction; E10's induced delay makes the window
observable but its offsets are artificial; the residual unprotected interval is
the gate's store read or the commit → release call (`recheck_to_publish_ms`),
removed only by a conditional command at the controller that owns the context.

## Experiment numbering: paper versus artifact

The paper has nine experiments, E1-E9, one per research question or subsection, with variants as labels
(E3-ABA, E4-PLC, E4-updates, E5-trace, E9-crash, ...); the testbed is a column of its experiment table, not a
new number. The scripts, data files, summary keys, and the detailed-results document keep the original
identifiers, which have gaps (there is no E2, E5, E7, or E19). `experiments.yaml` maps every paper
identifier to the artifact identifiers, scripts, and data files, and `python scripts/reproduce.py E4` resolves
one. Correspondence, paper -> artifact:

| Paper | Artifact |
|---|---|
| E1 | E1 |
| E1-classes / E1-ros2 / E1-policy | E14 / E8 (Gazebo, HTTP stack) / E17 |
| E2 | E9, E15 |
| E3 (-deadline, -ABA, -stop) | E10 (E10-deadline, E10-ABA), E27 |
| E4 (-ABA) | E21 (E21-ABA), with P and L from E22 |
| E4-PLC | E24 |
| E4-updates | E26 |
| E5 (-trace, -scale) | E16 (E16-trace), with E11; E28 |
| E6 | E20 |
| E7 | E25 (E25b: Eq. (4) check) |
| E8 | E4, E11, E12, E23 |
| E9-impair / E9-crash | E6 / E18 |
| E9-lifecycle | E29 |
| regression suites (no paper ID) | E0, E3, E13 |
