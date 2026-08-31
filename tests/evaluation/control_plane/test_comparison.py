from __future__ import annotations

from pathlib import Path

import pytest

from redis_sre_agent.evaluation.control_plane.comparison import (
    EvalComparisonService,
    InvalidEvalComparisonError,
)
from redis_sre_agent.evaluation.control_plane.models import EvalRunRecord, EvalRunStatus
from redis_sre_agent.evaluation.control_plane.store import FileEvalRunStore
from redis_sre_agent.evaluation.report_schema import EvalReportBundle


def _completed(store: FileEvalRunStore, run_id: str, suite_id: str, score: float) -> EvalRunRecord:
    record = EvalRunRecord(
        run_id=run_id,
        suite_id=suite_id,
        suite_name=suite_id,
        suite_manifest=f"{suite_id}.yaml",
        suite_digest="digest",
        scenario_ids=["scenario"],
        git_sha="abc",
        status=EvalRunStatus.COMPLETED,
    )
    store.save(record)
    report_dir = store.run_dir(run_id) / record.report_root / "scenario"
    report_dir.mkdir(parents=True)
    bundle = EvalReportBundle(
        scenario_id="scenario",
        git_sha="abc",
        execution_lane="agent_only",
        overall_pass=True,
        judge_scores={
            "overall_score": score,
            "criteria_scores": {},
            "detailed_feedback": "fixture",
            "passed": True,
        },
    )
    (report_dir / "report.json").write_text(bundle.model_dump_json(), encoding="utf-8")
    return record


def test_comparison_service_resolves_run_ids(tmp_path: Path) -> None:
    store = FileEvalRunStore(tmp_path)
    baseline = _completed(store, "01J00000000000000000000001", "suite", 70)
    candidate = _completed(store, "01J00000000000000000000002", "suite", 80)

    result = EvalComparisonService(store).compare(baseline.run_id, candidate.run_id)

    assert result.suite_id == "suite"
    assert result.summary.judge_score_delta == 10


def test_comparison_service_rejects_different_suites(tmp_path: Path) -> None:
    store = FileEvalRunStore(tmp_path)
    baseline = _completed(store, "01J00000000000000000000001", "suite-a", 70)
    candidate = _completed(store, "01J00000000000000000000002", "suite-b", 80)

    with pytest.raises(InvalidEvalComparisonError, match="same suite"):
        EvalComparisonService(store).compare(baseline.run_id, candidate.run_id)

