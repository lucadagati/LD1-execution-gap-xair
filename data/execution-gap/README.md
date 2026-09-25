# Frozen dataset — execution gap / AIS / XAIR

Per-trial data behind every number, table, and figure of the manuscript. The top-level
files were produced on 2026-09-23 on one host (see `environment.txt`; the host was shared,
load average 68–75). Two sub-directories hold the campaigns of 2026-09-24/25:

- `pinned/`: E4 (three runs) and E12 on reserved cores with a dedicated Redis
  (`scripts/run_pinned_perf.sh`);
- `distributed/`: E1, E1c, E9, E10 (optimistic and atomic release, induced and natural
  window), E12, E16, and E16-trace on the four-container testbed with netem delay
  (`scripts/run_distributed.sh`, `docker/distributed/compose.yml`).

Each sub-directory has its own `environment.txt`; both are frozen with
`scripts/freeze_subsets.sh`, and `paper_metrics_summary.json` summarizes them under the
keys `pinned` and `distributed`. E16-trace replays the UCI "Condition monitoring of
hydraulic systems" recording (Helwig, Pignanelli, Schütze; DOI 10.24432/C5CW21, CC BY 4.0),
which is downloaded on first use and verified by SHA-256
(`24128aad2ee45eea7e6b63ebbd9992cdf25d0483a2cebefbfc13bc69079af1f2`); the recording itself
is not redistributed here.

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

Code: the top-level data were generated with the code of release **v1.0.0**; changes
committed after those runs that touch the recorded paths are the atomic release endpoint,
the predicate-only gate scope, and extra version columns in E10, all added as new options
whose defaults leave the v1.0.0 behavior unchanged. The sub-directories were generated with
release **v1.1.0**.

Results of the originally submitted campaign (2026-09-10) are not part of this dataset;
their differences from these results are documented in the project's internal review.
