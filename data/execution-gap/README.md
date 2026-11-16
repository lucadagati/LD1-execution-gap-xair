# Frozen dataset — execution gap / AIS / XAIR

Per-trial data behind every number, table, and figure of the paper and of
`docs/detailed-results/`. The top-level
files were produced on 2026-09-23 on one host (see `environment.txt`; the host was shared,
load average 68–75). Seven sub-directories hold the later campaigns (2026-09-24 to 2026-10-02;
the distributed, physical, LAN, OpenPLC, and embedded subsets were rerun on 2026-10-02 with the current code):

- `pinned/`: E4 (three runs) and E12 on reserved cores with a dedicated Redis
  (`scripts/run_pinned_perf.sh`);
- `distributed/`: campaign 1 on the container testbed (Redis, policy engine, XAIR, gateway,
  OPC UA cell controller, producers; shaped delay): E1 (with the policy engine), E1c, E9,
  E10 (optimistic and atomic commit, induced and natural window, shared-clock positions),
  E10-deadline (four release paths), E10-ABA, E12 (with the policy engine), E16 and E16-trace
  (global, read-set, predicate-truth, predicate-only, policy engine), E17, E18, E18b, E20
  (4TU production log, with the decision-model and proxy sensitivity), E21 and E21-ABA (cell controller: unconditional, local interlock, version-conditional); `distributed/campaigns/c2..c5`: independent
  repetitions of E10, E10-deadline, E10-ABA, E16, E16-trace, E21, E21-ABA; `distributed/sensitivity/`:
  2 ± 1 ms delay, shared CPUs, phase-locked E16-trace. Produced by
  `scripts/run_distributed_campaigns.sh`; E18b is host-side (`experiments/run_e18b_durability.py`).
- `scaling/`: E23, cost of predicate-truth versions with 0–10 000 registered predicates,
  document vs hash layout (`experiments/run_e23_truth_scaling.py`, dedicated Redis container).
- `openplc/`: E24, conditional actuation on an OpenPLC v3 runtime over Modbus
  (`scripts/run_openplc.sh`, program `docker/openplc/cell_ctl.st`).
- `physical/`: E22, gateway and cell controller on a separate machine over a wide-area overlay
  (`scripts/run_physical.sh`): E1, E1c, E10 natural window, E10-ABA, E16, E21, E21-ABA.
- `lan/`: E22 on a Wi-Fi LAN (`scripts/run_lan.sh`): XAIR, Redis, and the producers on a Raspberry Pi 4,
  gateway and cell controller on the Apple M4 machine; same suites as `physical/`.
- `embedded/<board>/`: E25, the runtime on a Raspberry Pi 4 (`rpi4`) and a VisionFive 2 RISC-V board (`vf2`), every component on the board (`scripts/run_embedded.sh`).

Each sub-directory has its own `environment.txt`; all are frozen with
`scripts/freeze_subsets.sh`, and `paper_metrics_summary.json` summarizes them under the
keys `pinned`, `distributed`, `physical`, `lan`, `scaling`, and `openplc`. E16-trace replays the UCI "Condition monitoring of
hydraulic systems" recording (Helwig, Pignanelli, Schütze; DOI 10.24432/C5CW21, CC BY 4.0),
which is downloaded on first use and verified by SHA-256
(`24128aad2ee45eea7e6b63ebbd9992cdf25d0483a2cebefbfc13bc69079af1f2`); the recording itself
is not redistributed here. E20 uses the 4TU "Production Analysis with Process Mining
Technology" log (Levy 2014, DOI 10.4121/uuid:68726926-5ac5-4fab-b873-ee76ea412399), likewise
downloaded on first use and verified by SHA-256 (`experiments/mes_trace.py`).

| File | Suite | Produced by |
|------|-------|-------------|
| `e0_lifecycle.json` | E0 lifecycle regressions | `scripts/run_paper_campaign.sh` |
| `e1_baselines.csv`, `e1_fpr.csv` | E1 stale actuation, valid controls | idem |
| `e3_conflict_http.csv` | E3 conflict tie-break | idem |
| `e4_load_http.csv` | E4 sequential load | idem |
| `e9_consistency_sweep.csv` | E9 remote writer × cache policy | idem |
| `e10_toctou.csv`, `e10_toctou_boundary.csv` | E10 residual interval | idem |
| `e11_stratified_seed{42,7,123}.csv` | E11 mixed labels | idem |
| `e12_scaling.csv` | E12 closed-loop scaling | idem |
| `e13_faults.csv` | E13 fault cases | idem |
| `e14_variants.csv` | E14 action classes | idem |
| `e16_context_churn.csv` | E16 global vs read-set version under churn | idem |
| `e15_opcua_hil.csv` | E15 OPC UA context path | `experiments/run_e15_opcua_hil.py --runs 30` |
| `e6_network.csv`, `e6_network_fresh10s.csv` | E6 kernel impairment (500 ms / 10 s window) | `scripts/run_e6_netns.sh` |
| `e8_gazebo_campaign{1,2}.csv` | E8 Gazebo cell, two campaigns | `scripts/run_e8_docker.sh 30 <n>` |
| `paper_metrics_summary.json` | all aggregates | `experiments/aggregate_experiment_results.py` |

Code: the subsets were recorded with successive versions of this runtime, and the code in
this tree reproduces all of them. Features added after the top-level runs (atomic commit,
predicate-only and predicate-truth scopes, release-time deadline semantics, server-side
policy, extra version columns in E10) are new options whose defaults leave the recorded
behavior unchanged: in the top-level suites the ages stay far below both bounds and no
policy is installed. Features added after the `pinned/`, `distributed/`, and `physical/`
runs (hash layout, predicate retirement, parse caching, linear-time relatedness test, used
by `scaling/`, `openplc/`, and `lan/`) change latency, not outcomes.

## Changes in v1.7 (2026-10-06)

- **Lifecycle.** Authorization, commit, release, and effect are now distinct states
  (`AUTHORIZED`, `COMMITTED`, `RELEASED`, `EFFECT_CONFIRMED` / `FAILED` / `UNKNOWN_EFFECT`,
  `WITHHELD`; `docs/AIS.md`). Release decisions, the quantity behind every SER and FPR in this
  dataset, are unchanged: the gate, the commit, and the controller decide as before, and only
  the names of the states that record them changed. The one behavioural change is an atomic-mode middleware call that raises: it now counts as released with an unknown effect (it counted as blocked); no frozen trial contains such an error (`atomic_actuation_error`). No per-trial file was edited.
- **Release-path hardening.** `margin_ms` of the atomic commit must be finite and >= 0, `/withheld` writes a tombstone only for a committed intent, the optimistic gate honours a supervisory revoke read with its snapshot, and the Redis commit script no longer treats a negative age bound as "no bound" (it used -1 as that sentinel). No frozen run is affected: the only commit margin used is 3 ms against a 100 ms deadline (E10-deadline), the clock bound is 0 everywhere, and no run revoked an intent.
- **Controller versions across restarts.** The emulated cell controller now never reuses a version after a restart (persisted with `--state-file`, otherwise seeded from the boot time in microseconds). The campaigns ran builds that started at 0 on every restart; no campaign restarted a controller during a run, so no frozen result depends on it. The OpenPLC program still starts at 0.
- **E0** (`e0_lifecycle.json`) was regenerated with the new lifecycle: 14 cases, all passing
  (the v1.6 file had 10, all passing). E0 is deterministic and in-process.
- **E20 paired replay** (`distributed/e20_paired/`): a new run on the distributed testbed,
  `SUITES=e20 OUT_SUB=distributed/e20_paired sudo ./scripts/run_distributed.sh 0.5ms 0.1ms`,
  i.e. `run_e20_mes.py --design paired --compression 4320 --intents 2000 --seed 20`. Every
  decision goes through all five gates with the same decision time, machine, and precondition.
  The v1.6 rotation replay (`distributed/e20_mes_replay.csv`, one gate per decision, 200 per gate
  and latency) is kept unchanged. The offline and sensitivity files the paired run wrote again
  are kept next to it and are identical to the v1.6 ones (same log, same seed).
- **Manifests.** Every campaign directory has a `MANIFEST.json` (`scripts/make_manifest.py`): the
  SHA-256 of each file, the environment record, and the producing command and seeds. For the
  v1.7 run it also records the commit, the working-tree diff digest, dependency versions, host,
  and container image ids and registry digests. The manifests of earlier campaigns are
  retroactive (`scripts/write_retro_manifests.sh`) and list what was not recorded at run time.
- **Container images** are pinned by digest in the run scripts and compose files
  (`redis:7-alpine@sha256:520775a4…`, Redis 7.4.11; `python:3.12-slim@sha256:2f17fc04…`;
  `openpolicyagent/opa:1.21.0@sha256:9e001051…`): the images present on the campaign host,
  created before the first campaign. Earlier runs referenced the tags only, so the digest is
  not a run-time record for them.
- **OpenPLC program**: register 9 (conveyor) was added to `docker/openplc/cell_ctl.st` so that
  the program applies the effect of `RESUME`; registers 0–8, the decision logic, and the result
  codes are unchanged, and `openplc/` was not rerun. The modified program was compiled and run on the OpenPLC runtime (`xair-openplc` image) on 2026-10-06: an unconditional command sets register 9, a pause clears it, and the interlock and the version condition refuse on a paused line with the v1.6 result codes.

Experiment identifiers in this directory, the scripts, and `docs/detailed-results` are the artifact's own; the paper groups them into nine experiments, E1--E9, with variants as labels (table in `experiments/EVALUATION.md`, machine-readable in `experiments.yaml`; `python scripts/reproduce.py E4` resolves one).

## Provenance and verification

Every subset has a `MANIFEST.json` with the SHA-256 of each data file (README files are documentation
and are not listed). `scripts/verify_artifact.sh` runs the unit tests and E0 and checks every manifest
(`scripts/make_manifest.py --check data`). The campaigns were recorded with successive versions of the
runtime, before the repository was published as a single snapshot, so a campaign cannot be tied to the
commit that produced it; manifests written with `--run` record the commit and, with `--code`, the
SHA-256 of the code files a run executed. The code in this snapshot reproduces every subset with its
default options (see "Code" above).


## Added in the revision (2026-10-15)

- `e26_controller_churn.csv`, `e26_controller_churn.scan.json`: valid conditional commands under churn of
  controller-owned state (paper E8-churn); `experiments/run_e26_controller_churn.py` with
  `scripts/cell_controller_multi.py`, one stack on a single host.
- `embedded/<board>/e16_eq3_seed{1,2,3}.csv`, `chrony_{before,after}.txt`: churn cells of E16 on the boards
  with 3 x 400 trials per cell (`scripts/run_embedded_eq3.sh`); `eq3_empirical.json` compares them with
  Eq. (3) (`experiments/analysis_eq3_empirical.py`).
- Paper identifiers map to artifact identifiers in `experiments.yaml`; `python scripts/reproduce.py E8`
  lists the scripts and data files of one experiment.
- `e27_stop_resume.csv` (paper E3-stop) and `e28_truth_scale.csv`, `e28_truth_scale.writer.json` (paper E5-scale), 2026-10-16: stop fence and truth versions under many registered predicates. `e28_touch01pct.*` (document, hash and per-root layouts, 0.1 % of the paths changed per update) and `e28_shard_touch10.*` (per-root layout, 10 %), 2026-10-31: per-root context layout (`XAIR_PREDICATE_LAYOUT=shard`), stop fence off.

## review/ (2026-10-29/30)

Data of the review round, in the layout of `experiments/results`: `physical/`, `lan/` and `embedded/<board>/` hold the two repeat
campaigns (`campaigns/c1`, `c2`) and the stop-fence throughput runs (`campaigns/fence0`, `fence1`), all with the stop fence off like the original
campaigns except `fence1`; `distributed/review/` the pause-duration sweep (`gaps`) and the sampled-replica runs (`sampling0/50/200`);
`embedded_eq3_instr/` (and `embedded_eq3_legacy/`, the first campaign's read order rebuilt as an ablation) the Eq. (3) check with the store reads of validation and gate and a Poisson writer; `e26_campaigns/` and
`openplc/campaigns/` the repeats of E4-updates and E4-PLC; `review/e27_eps*_prod*.csv` the stop fence with a skewed producer clock. The
deadline runs with an emulated producer offset are the `e10_deadline_eps*_prod*_d*.csv` files of the campaign directories. Tables and numbers:
`python experiments/analyze_review.py --results data/execution-gap/review`. The OpenPLC program with an epoch is verified by
`openplc/plc_wrap.json` and `openplc/e24_openplc_epoch.csv`.
