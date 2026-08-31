"""Resolve historical runs and delegate report comparison to the eval engine."""

from __future__ import annotations

from pathlib import Path

from redis_sre_agent.evaluation.live_suite import compare_live_eval_reports

from .models import EvalRunComparison, EvalRunStatus
from .store import FileEvalRunStore


class InvalidEvalComparisonError(ValueError):
    pass


class EvalComparisonService:
    def __init__(self, store: FileEvalRunStore) -> None:
        self.store = store

    def _report_root(self, run_id: str, relative: str) -> Path:
        run_dir = self.store.run_dir(run_id).resolve()
        report_root = (run_dir / relative).resolve()
        if report_root != run_dir and run_dir not in report_root.parents:
            raise InvalidEvalComparisonError("report path escapes the run directory")
        return report_root

    def compare(self, baseline_run_id: str, candidate_run_id: str) -> EvalRunComparison:
        if baseline_run_id == candidate_run_id:
            raise InvalidEvalComparisonError("baseline and candidate runs must be different")
        baseline = self.store.get(baseline_run_id)
        candidate = self.store.get(candidate_run_id)
        if baseline.status is not EvalRunStatus.COMPLETED:
            raise InvalidEvalComparisonError("baseline run is not completed")
        if candidate.status is not EvalRunStatus.COMPLETED:
            raise InvalidEvalComparisonError("candidate run is not completed")
        if baseline.suite_id != candidate.suite_id:
            raise InvalidEvalComparisonError("baseline and candidate must use the same suite")
        if baseline.scenario_ids != candidate.scenario_ids:
            raise InvalidEvalComparisonError(
                "baseline and candidate must use the same scenarios in the same order"
            )

        summary = compare_live_eval_reports(
            self._report_root(baseline.run_id, baseline.report_root),
            self._report_root(candidate.run_id, candidate.report_root),
            baseline_policy=candidate.baseline_policy_snapshot,
        )
        return EvalRunComparison(
            baseline_run_id=baseline.run_id,
            candidate_run_id=candidate.run_id,
            suite_id=candidate.suite_id,
            policy=candidate.baseline_policy_snapshot,
            summary=summary,
        )


__all__ = ["EvalComparisonService", "InvalidEvalComparisonError"]
