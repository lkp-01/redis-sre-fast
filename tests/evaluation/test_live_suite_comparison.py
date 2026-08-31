from __future__ import annotations

from pathlib import Path

from redis_sre_agent.evaluation.live_suite import compare_live_eval_reports
from redis_sre_agent.evaluation.report_schema import EvalBaselinePolicy, EvalReportBundle


def _write_report(
    root: Path,
    scenario_id: str,
    *,
    passed: bool,
    score: float,
) -> None:
    report_dir = root / scenario_id
    report_dir.mkdir(parents=True, exist_ok=True)
    bundle = EvalReportBundle(
        scenario_id=scenario_id,
        git_sha="abc123",
        execution_lane="agent_only",
        overall_pass=passed,
        judge_scores={
            "overall_score": score,
            "criteria_scores": {},
            "detailed_feedback": "fixture",
            "passed": passed,
        },
    )
    (report_dir / "report.json").write_text(bundle.model_dump_json(), encoding="utf-8")


def test_comparison_reports_scenario_and_aggregate_deltas(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_report(baseline, "stable", passed=True, score=80)
    _write_report(baseline, "improved", passed=False, score=60)
    _write_report(candidate, "stable", passed=True, score=82)
    _write_report(candidate, "improved", passed=True, score=75)

    result = compare_live_eval_reports(
        baseline,
        candidate,
        baseline_policy=EvalBaselinePolicy(judge_score_variance_band=3),
    )

    rows = {row.scenario_id: row for row in result.rows}
    assert rows["stable"].score_delta == 2
    assert rows["stable"].classification == "unchanged"
    assert rows["improved"].baseline_pass is False
    assert rows["improved"].candidate_pass is True
    assert rows["improved"].classification == "improvement"
    assert result.baseline_pass_rate == 0.5
    assert result.candidate_pass_rate == 1.0
    assert result.pass_rate_delta == 0.5
    assert result.baseline_judge_score == 70
    assert result.candidate_judge_score == 78.5
    assert result.judge_score_delta == 8.5
    assert result.regression_count == 0
    assert result.improvement_count == 1


def test_comparison_marks_missing_candidate_as_regression(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_report(baseline, "removed", passed=True, score=90)
    candidate.mkdir()

    result = compare_live_eval_reports(baseline, candidate)

    row = result.rows[0]
    assert row.classification == "removed"
    assert row.passed is False
    assert result.regression_count == 1
    assert result.passed is False


def test_comparison_does_not_pass_when_both_report_directories_are_empty(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()

    result = compare_live_eval_reports(baseline, candidate)

    assert result.rows == []
    assert result.passed is False
