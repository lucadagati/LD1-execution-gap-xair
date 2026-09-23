# Frozen dataset — execution gap / AIS / XAIR

Per-trial data behind every number, table, and figure of the manuscript, produced on
2026-09-23 on one host (see `environment.txt`; the host was shared, load average 68–75).

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

Code: the data were generated with the code of release **v1.0.0**; changes committed
after the runs are limited to documentation, table generation
(`experiments/make_paper_tables.py`), and the process handling of the stack start/stop
scripts, none of which affects the recorded outcomes.

Results of the originally submitted campaign (2026-09-10) are not part of this dataset;
their differences from these results are documented in the project's internal review.
