# Eval Control Plane

The eval control plane is an opt-in orchestration layer over the existing live-eval engine. It
discovers trusted suite manifests, starts isolated live-eval workers, persists historical runs,
and compares a completed baseline run with a completed candidate run.

## Enable locally

Install the optional runtime dependency and enable the feature in the **same PowerShell session**
that starts Uvicorn:

```powershell
uv sync --extra eval-control
$env:EVAL_CONTROL_ENABLED = "true"
uv run uvicorn redis_sre_agent.api.app:app --reload
```

If Uvicorn is already running, stop it and start it again after setting the variable. A running
process does not reload environment variables from another terminal. For a persistent local
setting, add `EVAL_CONTROL_ENABLED=true` to the ignored `.env` file, then restart Uvicorn.

Docker must be available because the existing live-eval runner starts an isolated Redis
Testcontainer. Suite manifests are discovered only below `EVAL_SUITE_ROOT` (default
`evals/suites`). Run state is written below `EVAL_RUN_ROOT` (default
`.artifacts/eval-control`).

API keys are loaded through the existing settings mechanism. They are never persisted in run
metadata. Run metadata records only model names, the LLM factory name, and the scheme/host/port
of the configured API base URL.

## API resources

- `GET /api/v1/evals/suites`
- `GET /api/v1/evals/suites/{suite_id}`
- `POST /api/v1/evals/runs` with `{ "suite_id": "live-agent-only-smoke" }`
- `POST /api/v1/evals/runs` with a trusted subset, for example
  `{ "suite_id": "live-agent-only-smoke", "scenario_ids": ["prompt/knowledge-agent-no-live-access"] }`
- `GET /api/v1/evals/runs`
- `GET /api/v1/evals/runs/{run_id}`
- `GET /api/v1/evals/runs/{run_id}/reports`
- `GET /api/v1/evals/runs/{run_id}/report?scenario_id=...`
- `POST /api/v1/evals/comparisons`

The run request intentionally accepts no file path, output path, branch, or API credential. A
suite must be present in the discovered Registry. When `scenario_ids` is omitted, all scenarios
in the suite run. When it is provided, it must be non-empty, contain no duplicates, and every ID
must belong to that suite. The persisted run keeps the selected IDs in suite-manifest order and
sets `is_partial=true` when the selection does not cover the full suite. Only one live eval may
run at a time. Comparisons require the same suite and identical scenario selections, so a partial
run cannot accidentally be compared with a full-suite baseline.

Execution status and evaluation quality are separate: a run can be `completed` while
`evaluation_passed` is `false`. `failed` means the worker infrastructure did not complete the
suite.

FastAPI exposes these contracts in `/docs`, which can be used as the first local operator
interface when API authentication is disabled. Auth-enabled deployments must use the existing
bearer-token client flow. A dedicated UI can remain a thin client: populate its suite selector
from `GET /suites`, poll `GET /runs` after starting a run, filter completed runs by suite for
baseline/candidate selectors, and render the scenario rows returned by `POST /comparisons`. No
suite IDs or thresholds need to be hard-coded in that client.

## Deployment boundary

Version 1 is single-host and expects a persistent local volume for the run root. The live eval
runs in a child Python process because the existing engine temporarily redirects process-global
Redis settings to its Testcontainer. Multi-host scheduling and automatic git checkout are not
supported.
