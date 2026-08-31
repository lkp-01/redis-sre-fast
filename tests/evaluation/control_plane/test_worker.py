from __future__ import annotations

import json
from pathlib import Path

import pytest

from redis_sre_agent.evaluation.control_plane.manager import EvalRunManager
from redis_sre_agent.evaluation.control_plane.models import EvalRunRecord, EvalRunStatus
from redis_sre_agent.evaluation.control_plane.registry import SuiteRegistry
from redis_sre_agent.evaluation.control_plane.store import FileEvalRunStore
from redis_sre_agent.evaluation.control_plane.worker import execute_run
from redis_sre_agent.evaluation.live_suite import LiveEvalSuiteSummary

from .test_registry import SCENARIO


def _registry(tmp_path: Path) -> SuiteRegistry:
    suites = tmp_path / "suites"
    scenarios = tmp_path / "scenarios"
    suites.mkdir()
    scenarios.mkdir()
    (scenarios / "scenario.yaml").write_text(SCENARIO, encoding="utf-8")
    (suites / "example.yaml").write_text(
        "suites:\n  example:\n    scenarios:\n      - ../scenarios/scenario.yaml\n",
        encoding="utf-8",
    )
    return SuiteRegistry(suites)


def _queued_run(registry: SuiteRegistry, store: FileEvalRunStore) -> EvalRunRecord:
    suite = registry.get("example")
    record = EvalRunRecord(
        run_id="01J00000000000000000000001",
        suite_id=suite.id,
        suite_name=suite.name,
        suite_manifest=suite.manifest,
        suite_digest=suite.digest,
        scenario_ids=[scenario.id for scenario in suite.scenarios],
        git_sha="abc123",
        baseline_policy_snapshot=suite.baseline_policy,
    )
    store.save(record)
    manager = EvalRunManager(registry, store, repo_root=Path.cwd())
    manager._acquire_lock(record.run_id)
    return record


@pytest.mark.asyncio
async def test_worker_persists_completed_quality_result(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")
    record = _queued_run(registry, store)

    async def fake_runner(*args, **kwargs):
        output_dir = Path(kwargs["output_dir"]) / "example"
        output_dir.mkdir(parents=True)
        (output_dir / "summary.json").write_text("{}", encoding="utf-8")
        return LiveEvalSuiteSummary(
            suite_name="example",
            config_path=str(registry.root / "example.yaml"),
            trigger="workflow_dispatch",
            git_sha="abc123",
            baseline_policy={},
            total_scenarios=1,
            passed_scenarios=0,
            failed_scenarios=1,
            pass_rate=0,
            judge_score=61,
            allowed_failed_scenarios=0,
            all_passed=False,
            output_dir=str(output_dir),
            completed_at="2026-01-01T00:00:00+00:00",
        )

    result = await execute_run(
        record.run_id,
        registry=registry,
        store=store,
        suite_runner=fake_runner,
    )

    assert result.status is EvalRunStatus.COMPLETED
    assert result.evaluation_passed is False
    assert result.pass_rate == 0
    assert result.judge_score == 61
    assert not (store.root / "active.lock").exists()


@pytest.mark.asyncio
async def test_worker_sanitizes_failure_without_storing_api_key(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")
    record = _queued_run(registry, store)

    async def failing_runner(*args, **kwargs):
        raise RuntimeError("authorization=deepseek-secret-value")

    result = await execute_run(
        record.run_id,
        registry=registry,
        store=store,
        suite_runner=failing_runner,
    )

    assert result.status is EvalRunStatus.FAILED
    assert "deepseek-secret-value" not in (result.error or "")
    persisted = json.loads((store.run_dir(record.run_id) / "run.json").read_text())
    assert "deepseek-secret-value" not in json.dumps(persisted)

