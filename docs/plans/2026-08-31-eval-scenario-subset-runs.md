# Eval Scenario Subset Runs Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow an eval control-plane run to execute one or more trusted scenarios from a registered suite while preserving full-suite behavior when no selection is supplied.

**Architecture:** Extend `POST /api/v1/evals/runs` with an optional non-empty `scenario_ids` array. Validate selections against the suite registry, persist the actual ordered selection and an `is_partial` marker, pass it to the existing live-suite runner, and reject comparisons whose selected scenarios differ.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, asyncio, pytest.

---

### Task 1: API contract and selection persistence

**Files:**
- Modify: `redis_sre_agent/api/evals.py`
- Modify: `redis_sre_agent/evaluation/control_plane/manager.py`
- Modify: `redis_sre_agent/evaluation/control_plane/models.py`
- Test: `tests/api/test_evals.py`
- Test: `tests/evaluation/control_plane/test_manager.py`

**Step 1: Write failing tests**

Add tests that post `scenario_ids`, reject an empty array and unknown IDs, and verify a manager-created subset run persists only the selected IDs with `is_partial=True`.

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/api/test_evals.py tests/evaluation/control_plane/test_manager.py -q`

Expected: failures because the request model and manager do not accept `scenario_ids`.

**Step 3: Implement minimal contract**

Add `scenario_ids: list[str] | None` to `CreateEvalRunRequest`, pass it to `create_run`, validate membership and duplicates in the manager, preserve suite order, and add a backwards-compatible `is_partial: bool = False` field to `EvalRunRecord`.

**Step 4: Run tests to verify pass**

Run: `uv run pytest tests/api/test_evals.py tests/evaluation/control_plane/test_manager.py -q`

Expected: all focused API and manager tests pass.

### Task 2: Live-suite filtering and worker propagation

**Files:**
- Modify: `redis_sre_agent/evaluation/live_suite/__init__.py`
- Modify: `redis_sre_agent/evaluation/control_plane/worker.py`
- Test: `tests/evaluation/test_live_suite_execution.py`
- Test: `tests/evaluation/control_plane/test_worker.py`

**Step 1: Write failing tests**

Add a live-suite test proving only selected scenarios are dispatched and a worker test proving persisted `scenario_ids` are forwarded to the runner.

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/evaluation/test_live_suite_execution.py tests/evaluation/control_plane/test_worker.py -q`

Expected: failures because `run_live_eval_suite` has no scenario filter.

**Step 3: Implement minimal filtering**

Add an optional `scenario_ids` argument to `run_live_eval_suite`, validate it against loaded suite scenarios, preserve manifest ordering, and have the worker pass the persisted selection.

**Step 4: Run tests to verify pass**

Run: `uv run pytest tests/evaluation/test_live_suite_execution.py tests/evaluation/control_plane/test_worker.py -q`

Expected: all focused runner and worker tests pass.

### Task 3: Comparison safety and operator documentation

**Files:**
- Modify: `redis_sre_agent/evaluation/control_plane/comparison.py`
- Modify: `docs/eval-control-plane.md`
- Test: `tests/evaluation/control_plane/test_comparison.py`

**Step 1: Write failing comparison test**

Add a test that creates two completed runs for the same suite with different `scenario_ids` and expects `InvalidEvalComparisonError`.

**Step 2: Run test to verify failure**

Run: `uv run pytest tests/evaluation/control_plane/test_comparison.py -q`

Expected: failure because comparison currently checks only `suite_id`.

**Step 3: Implement and document**

Require identical ordered scenario selections for comparisons. Document full-suite and subset request examples, membership validation, `is_partial`, and comparison restrictions.

**Step 4: Run all affected tests**

Run: `uv run pytest tests/api/test_evals.py tests/evaluation/control_plane tests/evaluation/test_live_suite_execution.py -q`

Expected: all affected tests pass.

### Task 4: Quality verification

**Files:**
- Verify all modified Python and Markdown files.

**Step 1: Run formatting and lint checks**

Run: `uv run ruff check redis_sre_agent/api/evals.py redis_sre_agent/evaluation/control_plane redis_sre_agent/evaluation/live_suite tests/api/test_evals.py tests/evaluation/control_plane tests/evaluation/test_live_suite_execution.py`

Expected: no lint errors.

**Step 2: Review the diff**

Run: `git diff --check` and `git diff --stat`.

Expected: no whitespace errors and only intended files changed.
