# XAIR — eXecution-time Action Intent Runtime

Reference implementation, experiment suites, and frozen data for the paper
*Execution-Time Validation of Context-Carrying Action Intents in Industrial CPS*
(IEEE Transactions on Industrial Informatics, under review).

XAIR validates **AIS action intents** (JSON Schema in `schemas/`) against a
versioned plant-context snapshot at validation time `t_v`; the actuator gateway
rechecks the intent's read-set version and predicates at `t_g`, immediately
before the middleware release at `t_r`.

## Layout

```
xair/            runtime package (core: validation, lifecycle FSM, context store; adapters: FastAPI server)
schemas/         AIS v1 JSON Schema
examples/        example intents
tests/           unit tests (+ opt-in HTTP integration tests)
scripts/         stack start/stop, actuator gateway, ROS witness, campaign and verification scripts
experiments/     one script per suite (run_e*.py), aggregation, figures, EVALUATION.md
simulation/      Gazebo industrial cell (E8) and OPC UA bridge (E15)
docker/          ROS 2 Jazzy + Gazebo Harmonic image for E8
data/execution-gap/   frozen per-trial data behind every number in the paper
journal/         manuscript — local only, git-ignored, never published
```

`experiments/results/` is the scratch output of any run (git-ignored);
`scripts/sync_paper_outputs.sh` freezes a complete campaign into `data/execution-gap/`.

## Quick start

```bash
./scripts/ensure_venv.sh          # .venv + pip install -e ".[dev]"
.venv/bin/python -m pytest -q tests
./scripts/start_full_stack.sh     # Redis (docker) + XAIR :8080 + gateway :9092
./scripts/verify_e2e.sh
./scripts/stop_full_stack.sh
```

Ports and endpoints are configurable (`XAIR_PORT`, `ADAPTER_HTTP_PORT`,
`ADAPTER_WS_PORT`, `REDIS_URL`; empty `REDIS_URL` = in-memory store). Example
for a shared host:

```bash
XAIR_PORT=18080 ADAPTER_HTTP_PORT=19092 ADAPTER_WS_PORT=19091 \
REDIS_URL=redis://127.0.0.1:6379/1 ./scripts/run_paper_campaign.sh
```

## Reproducing the paper

| Step | Command | Output |
|------|---------|--------|
| HTTP campaign (E0–E4, E9–E14, ~25 min) | `./scripts/run_paper_campaign.sh` | `experiments/results/` |
| Freeze + summary + figures + generated tables | `./scripts/sync_paper_outputs.sh` | `data/execution-gap/` (and `journal/` locally) |
| E8-Gazebo in container (ROS 2 Jazzy + Gazebo Harmonic) | `./scripts/run_e8_docker.sh 30 <tag>` | `experiments/results/e8_gazebo_campaign<tag>.csv` |
| E8-Gazebo on a native Jazzy host | `./scripts/run_e8_gazebo_full.sh 30 <tag>` | same |
| E6 netem in a network namespace (root) | `sudo ./scripts/run_e6_netns.sh 30 10 500` | `experiments/results/e6_network.csv` |
| E15 OPC UA (needs `asyncua`) | `.venv/bin/python experiments/run_e15_opcua_hil.py --runs 30` | `experiments/results/e15_opcua_hil.csv` |
| Smoke check (scratch dir) | `./scripts/verify_reproduction.sh` | temp dir |
| Release check | `./scripts/verify_release.sh <tag>` | — |

Suite definitions, metrics, and data provenance are in
[experiments/EVALUATION.md](experiments/EVALUATION.md).

## Publishing

The layout is identical to
<https://github.com/lucadagati/LD1-execution-gap-xair>, so this
directory can be used directly as its git working tree (no separate "flat"
export step). `journal/` is in `.gitignore`; `verify_release.sh` fails if it is
ever tracked.

## License

Apache-2.0 — see [LICENSE](LICENSE). Citation metadata: [CITATION.cff](CITATION.cff).
