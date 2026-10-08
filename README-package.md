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

## Differences from the Zenodo code package

This repository is based on the code package published on Zenodo (DOI 10.5281/zenodo.23178244), with
three kinds of changes. None of them is intended to change what the code does.

- **Neutral identifiers.** An internal project prefix in identifiers was replaced by `OBS` / `obs`:
  environment variables are now `OBS_*`, Redis keys are now `obs:*`, and the Docker Compose project of
  the ablation stack, temporary directories, lock files, the logrotate file, the tmux session and the
  log markers (`[OBS ...]`) follow the same scheme. Code that talks to the Redis queues of the Zenodo
  package, or that reads its log markers, must map these names; the original names are in that package.
- **Comments and docstrings.** References to the author's private notes (known-limitation records,
  decision logs, design notes) were removed. The explanations that matter for the study are in the
  preprint.
- **Packaging.** The package name in `pyproject.toml` follows the repository name.

## Running the central stack

`proposed/docker-compose.yml` expects two files that are not included because they hold credentials.
Create them next to the Compose file:

1. `proposed/.env`

   ```
   CLICKHOUSE_USER=default
   CLICKHOUSE_PASSWORD=<choose a password>
   ```

2. `proposed/clickhouse-users.xml`, with the SHA-256 of the same password
   (`echo -n '<password>' | sha256sum`):

   ```xml
   <clickhouse>
     <users>
       <default>
         <password remove="1"/>
         <password_sha256_hex>PASTE-THE-64-HEX-DIGEST-HERE</password_sha256_hex>
         <networks><ip>::/0</ip></networks>
         <profile>default</profile>
         <quota>default</quota>
         <access_management>0</access_management>
       </default>
     </users>
   </clickhouse>
   ```

   `<password remove="1"/>` is required: the base image already defines an empty password, and adding
   a hash without removing it makes ClickHouse refuse to start. `<ip>::/0</ip>` accepts connections
   from any address, because the other containers reach ClickHouse over the Compose network. The
   Compose file also publishes ports 8123 (ClickHouse HTTP) and 8000 (receiver) on all host
   interfaces, so on a host that is reachable from outside, restrict them with a firewall or bind
   them to `127.0.0.1` in the `ports:` entries.

The Compose files use floating image tags (`clickhouse-server:latest`, `vector:latest-alpine`,
`redis:alpine`); pin the versions you tested if you need an exact reproduction. The Compose stacks
were not started while preparing this repository, because Docker was not available there.

## Tests

The full unit-test suite passes on Linux: 232 tests (Ubuntu under WSL2, Python 3.14.4, torch 2.14.1
for CPU, shap 0.52.0, plus the packages in `requirements.txt`). Run it with `python -m pytest`. On
Windows, a number of tests of the shell scripts fail because they rely on POSIX behavior (for example
`chmod` on stub executables), and the two Fidelity Guard test modules need `torch`; use Linux.

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
