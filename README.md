# XAIR — eXecution-time Action Intent Runtime

Reference implementation and evaluation harness for **execution-time validation** of context-carrying action intents (AIS) in industrial cyber-physical systems.

Companion paper: *Execution-Time Validation of Context-Carrying Action Intents in Industrial CPS* (IEEE TII submission).

## Repository layout

| Path | Contents |
|------|----------|
| `xair/` | Core runtime: validation, lifecycle, HTTP adapter |
| `experiments/` | E0–E15 evaluation drivers, aggregation, plotting |
| `tests/` | Unit and HTTP integration tests |
| `simulation/` | Gazebo industrial cell + OPC UA HIL helpers |
| `schemas/` | AIS JSON Schema |
| `scripts/` | Stack startup, paper campaign, reproduction checks |

## Quick start

```bash
git clone https://github.com/lucadagati/XAIR_eXecution-time_Action_Intent_Runtime.git
cd XAIR_eXecution-time_Action_Intent_Runtime

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Unit tests + lifecycle smoke (E0)
.venv/bin/pytest tests/ -v
.venv/bin/python experiments/run_e0_lifecycle.py

# Full HTTP stack (Redis + XAIR :8080 + adapter :9092)
./scripts/start_full_stack.sh
./scripts/verify_e2e.sh
```

## Paper evaluation campaign

Regenerates canonical CSV/JSON under `experiments/results/`:

```bash
./scripts/run_paper_campaign.sh
.venv/bin/python experiments/aggregate_experiment_results.py
.venv/bin/python experiments/plot_results.py
```

See [REPRODUCE.md](REPRODUCE.md) for suite-by-suite commands, Gazebo E8 setup, and measurement protocol notes.

## Requirements

- Python 3.12+
- Docker (optional, for Redis via `docker-compose.yml`)
- Optional: ROS 2 Jazzy + Gazebo Harmonic for E8 cell simulation
- Optional: `asyncua` (included in `[dev]`) for E15 OPC UA path

## Citation

```bibtex
@software{xair_runtime_2026,
  author = {D'Agati, Luca and Tricomi, Giuseppe and Mirto, Fabio Orazio and Merlino, Giovanni},
  title = {{XAIR Runtime: Execution-Time Validation for Industrial CPS}},
  year = {2026},
  url = {https://github.com/lucadagati/XAIR_eXecution-time_Action_Intent_Runtime}
}
```

License: Apache-2.0 — see [LICENSE](LICENSE).
