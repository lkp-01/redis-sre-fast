from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from redis_sre_agent.evaluation.control_plane import manager as manager_module
from redis_sre_agent.evaluation.control_plane.manager import (
    ActiveEvalRunError,
    EvalRunManager,
    InvalidScenarioSelectionError,
)
from redis_sre_agent.evaluation.control_plane.models import EvalRunRecord, EvalRunStatus
from redis_sre_agent.evaluation.control_plane.store import FileEvalRunStore

from .test_worker import _registry


def _two_scenario_registry(tmp_path: Path):
    registry = _registry(tmp_path)
    suite = registry.get("example")
    second = suite.scenarios[0].model_copy(
        update={"id": "prompt/second", "name": "Second scenario"}
    )
    third = suite.scenarios[0].model_copy(
        update={"id": "prompt/third", "name": "Third scenario"}
    )
    expanded = suite.model_copy(
        update={
            "scenario_count": 3,
            "scenarios": [suite.scenarios[0], second, third],
        }
    )

    class StaticRegistry:
        def get(self, suite_id: str):
            if suite_id != expanded.id:
                raise KeyError(suite_id)
            return expanded

    return StaticRegistry()


@pytest.mark.asyncio
async def test_manager_allows_only_one_active_run(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")
    blocker = asyncio.Event()

    async def blocked_launcher(run_id: str) -> None:
        await blocker.wait()

    manager = EvalRunManager(
        registry,
        store,
        repo_root=Path.cwd(),
        process_launcher=blocked_launcher,
    )
    first = await manager.create_run("example", requested_by="operator")

    with pytest.raises(ActiveEvalRunError) as error:
        await manager.create_run("example")

    assert error.value.run_id == first.run_id
    assert first.requested_by == "operator"
    assert "main" in first.effective_config.agent_models
    assert first.status is EvalRunStatus.QUEUED
    assert first.is_partial is False
    blocker.set()
    await manager.shutdown()
    manager.release_lock(first.run_id)


@pytest.mark.asyncio
async def test_manager_persists_an_ordered_partial_scenario_selection(tmp_path: Path) -> None:
    registry = _two_scenario_registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")

    async def no_op_launcher(run_id: str) -> None:
        return None

    manager = EvalRunManager(
        registry,  # type: ignore[arg-type]
        store,
        repo_root=Path.cwd(),
        process_launcher=no_op_launcher,
    )

    record = await manager.create_run(
        "example",
        scenario_ids=["prompt/second", "prompt/example"],
    )
    await asyncio.sleep(0)

    assert record.scenario_ids == ["prompt/example", "prompt/second"]
    assert record.is_partial is True
    manager.release_lock(record.run_id)


@pytest.mark.asyncio
async def test_manager_rejects_unknown_or_duplicate_scenario_ids(tmp_path: Path) -> None:
    registry = _two_scenario_registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")
    manager = EvalRunManager(registry, store, repo_root=Path.cwd())  # type: ignore[arg-type]

    with pytest.raises(InvalidScenarioSelectionError, match="unknown"):
        await manager.create_run("example", scenario_ids=["prompt/missing"])
    with pytest.raises(InvalidScenarioSelectionError, match="duplicate"):
        await manager.create_run(
            "example",
            scenario_ids=["prompt/example", "prompt/example"],
        )

    assert not manager.lock_path.exists()


@pytest.mark.asyncio
async def test_recovery_marks_unowned_active_run_interrupted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")

    async def no_op_launcher(run_id: str) -> None:
        return None

    manager = EvalRunManager(
        registry,
        store,
        repo_root=Path.cwd(),
        process_launcher=no_op_launcher,
    )
    record = await manager.create_run("example")
    await asyncio.sleep(0)
    manager.release_lock(record.run_id)

    await manager.recover()

    assert store.get(record.run_id).status is EvalRunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_launcher_failure_marks_run_failed_and_releases_lock(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")

    async def failing_launcher(run_id: str) -> None:
        raise RuntimeError("process launch failed")

    manager = EvalRunManager(
        registry,
        store,
        repo_root=Path.cwd(),
        process_launcher=failing_launcher,
    )
    record = await manager.create_run("example")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    persisted = store.get(record.run_id)
    assert persisted.status is EvalRunStatus.FAILED
    assert persisted.error == "process launch failed"
    assert not manager.lock_path.exists()


@pytest.mark.asyncio
async def test_recovery_removes_malformed_stale_lock(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")
    manager = EvalRunManager(registry, store, repo_root=Path.cwd())
    manager.lock_path.write_text("{", encoding="utf-8")

    await manager.recover()

    assert not manager.lock_path.exists()


@pytest.mark.asyncio
async def test_worker_launch_uses_popen_not_asyncio_subprocess(tmp_path: Path, monkeypatch) -> None:
    registry = _registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")
    suite = registry.get("example")
    record = EvalRunRecord(
        run_id="01J00000000000000000000001",
        suite_id=suite.id,
        suite_name=suite.name,
        suite_manifest=suite.manifest,
        suite_digest=suite.digest,
        scenario_ids=[scenario.id for scenario in suite.scenarios],
        git_sha="abc123",
    )
    store.save(record)
    manager = EvalRunManager(registry, store, repo_root=Path.cwd())
    manager._acquire_lock(record.run_id)
    calls = []

    class FakeProcess:
        pid = 4321

        def wait(self) -> int:
            return 0

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(manager_module.subprocess, "Popen", fake_popen)

    await manager._spawn_worker(record.run_id)

    assert calls
    assert "redis_sre_agent.evaluation.control_plane.worker" in calls[0][0][0]
    assert manager._read_lock()["pid"] == 4321
    manager.release_lock(record.run_id)


@pytest.mark.asyncio
async def test_empty_launcher_exception_records_exception_type(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    store = FileEvalRunStore(tmp_path / "control")

    async def failing_launcher(run_id: str) -> None:
        raise NotImplementedError()

    manager = EvalRunManager(
        registry,
        store,
        repo_root=Path.cwd(),
        process_launcher=failing_launcher,
    )
    record = await manager.create_run("example")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert store.get(record.run_id).error == "NotImplementedError"
