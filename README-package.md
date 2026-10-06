# Reproducibility package

Supplementary code for the preprint *Self-Proving Data Integrity for a Distributed, Autonomous
Observation Platform* (two-site, 90-day honeypot observation, 2026-07-02 to 2026-09-30).

## Contents

- `src/` - the receiver (FastAPI) and worker, the analysis code (loss-rate measurement,
  periodicity analysis, statistical tests, Fidelity Guard), and the resource monitor.
- `proposed/` - the central-node stack (Docker Compose, Vector configuration, ClickHouse schema)
  and the isolated stack of the aggregation-window ablation.
- `edge/` - the edge-node stack (Docker Compose, Vector configuration).
- `baseline/` - the Elasticsearch/Logstash/Kibana measurement target of the controlled experiment.
- `scripts/` - experiment, backup and analysis scripts, including `analyze_periodicity_final.py`
  (the pre-registered periodicity analysis).
- `tests/` - unit tests.

## What was removed or replaced

Server addresses, host names, user names, the SSH port and the database credentials (including
the password hash) were removed or replaced with placeholders. Addresses are the documentation
addresses 203.0.113.10 and 203.0.113.11 (RFC 5737). Operational files that only make sense on the
original servers (crontab copies, repository hooks and issue-sync tooling) are not included.
Row-level data and raw honeypot logs are not included (see the paper, Data and Code Availability).

## License

The code in this package is released under the MIT License (see `LICENSE`). The accompanying
preprint is released separately under CC BY 4.0.

## Notes

- `baseline/` is the measurement target of the controlled experiment. Its Elasticsearch runs with
  security disabled and its ports published. Use it for the experiment only, never on an exposed host.
- `scripts/run_multiedge_experiment.sh` refuses to run against the production address of the
  original central node. In this copy that address is a placeholder, so the guard protects
  nothing. Do not point the load-generating scripts (`scripts/run_multiedge_experiment.sh`,
  `src/generator/spike.py`) at infrastructure that you do not own.
- Install the dependencies in `requirements.txt` before running `pytest tests`; several test
  modules need FastAPI, Redis and ClickHouse client libraries.
