from __future__ import annotations

from pathlib import Path

import pytest

from redis_sre_agent.evaluation.control_plane.models import EvalRunRecord, EvalRunStatus
from redis_sre_agent.evaluation.control_plane.store import FileEvalRunStore


def _record(run_id: str, suite_id: str = "suite-a") -> EvalRunRecord:
    return EvalRunRecord(
        run_id=run_id,
        suite_id=suite_id,
        suite_name=suite_id,
        suite_manifest=f"{suite_id}.yaml",
        suite_digest="digest",
        scenario_ids=["scenario-a"],
        git_sha="abc123",
        status=EvalRunStatus.QUEUED,
    )


def test_store_persists_and_filters_runs(tmp_path: Path) -> None:
    store = FileEvalRunStore(tmp_path)
    store.save(_record("01J00000000000000000000001", "suite-a"))
    second = _record("01J00000000000000000000002", "suite-b")
    second.status = EvalRunStatus.COMPLETED
    store.save(second)

    assert store.get(second.run_id).status is EvalRunStatus.COMPLETED
    page = store.list(suite_id="suite-b", status=EvalRunStatus.COMPLETED)
    assert [item.run_id for item in page.items] == [second.run_id]


def test_store_rejects_path_like_run_ids(tmp_path: Path) -> None:
    store = FileEvalRunStore(tmp_path)

    with pytest.raises(ValueError, match="invalid run id"):
        store.get("../escape")


def test_store_skips_corrupt_run_manifests(tmp_path: Path) -> None:
    store = FileEvalRunStore(tmp_path)
    store.save(_record("01J00000000000000000000001"))
    corrupt = tmp_path / "runs" / "01J00000000000000000000002"
    corrupt.mkdir(parents=True)
    (corrupt / "run.json").write_text("{", encoding="utf-8")

    page = store.list()

    assert len(page.items) == 1
    assert len(page.errors) == 1

