"""Subprocess entrypoint that executes one trusted eval run."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from redis_sre_agent.core.config import settings
from redis_sre_agent.evaluation.live_suite import run_live_eval_suite

from .manager import (
    EvalRunManager,
    capture_effective_config,
    capture_git_state,
    describe_run_error,
)
from .models import EvalRunRecord, EvalRunStatus, utc_now_iso
from .registry import SuiteRegistry
from .store import FileEvalRunStore

logger = logging.getLogger(__name__)
SuiteRunner = Callable[..., Awaitable[Any]]


async def execute_run(
    run_id: str,
    *,
    registry: SuiteRegistry,
    store: FileEvalRunStore,
    suite_runner: SuiteRunner = run_live_eval_suite,
) -> EvalRunRecord:
    manager = EvalRunManager(registry, store, repo_root=Path.cwd())
    record = store.get(run_id)
    try:
        suite = registry.get(record.suite_id)
        if suite.digest != record.suite_digest:
            raise RuntimeError("suite definition changed after the run was queued")

        record.status = EvalRunStatus.RUNNING
        record.started_at = utc_now_iso()
        record.worker_pid = os.getpid()
        record.git_sha, record.git_dirty, record.git_diff_digest = capture_git_state(Path.cwd())
        record.effective_config = capture_effective_config(
            baseline_profile=suite.baseline_profile,
            threshold=suite.judge_pass_threshold,
        )
        store.save(record)
        manager.update_lock_pid(run_id, os.getpid())

        summary = await suite_runner(
            suite.id,
            config_path=suite.config_path,
            output_dir=store.run_dir(run_id) / record.report_root,
            event_name="manual",
            model_name=settings.openai_model,
            baseline_profile=suite.baseline_profile,
            update_baseline=False,
            session_id_prefix=f"eval-run-{run_id}",
            user_id=f"eval-control-plane::{record.requested_by}",
            scenario_ids=record.scenario_ids or None,
        )
        record.status = EvalRunStatus.COMPLETED
        record.completed_at = summary.completed_at or utc_now_iso()
        record.summary_path = str(
            Path(record.report_root) / suite.name / "summary.json"
        ).replace("\\", "/")
        record.total_scenarios = summary.total_scenarios
        record.passed_scenarios = summary.passed_scenarios
        record.failed_scenarios = summary.failed_scenarios
        record.pass_rate = summary.pass_rate
        record.judge_score = summary.judge_score
        record.evaluation_passed = summary.all_passed
        record.baseline_policy_snapshot = summary.baseline_policy
        if summary.git_sha != record.git_sha:
            record.metadata["git_sha_changed_during_run"] = True
            record.metadata["worker_start_git_sha"] = record.git_sha
            record.git_sha = summary.git_sha
        record.error = None
    except Exception as exc:
        logger.error(
            "eval run failed run_id=%s suite_id=%s error=%s",
            run_id,
            record.suite_id,
            describe_run_error(exc),
        )
        record.status = EvalRunStatus.FAILED
        record.completed_at = utc_now_iso()
        record.error = describe_run_error(exc)
    finally:
        store.save(record)
        manager.release_lock(run_id)
    return record


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute one eval control-plane run")
    parser.add_argument("--root", required=True)
    parser.add_argument("--suite-root", required=True)
    parser.add_argument("--run-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    store = FileEvalRunStore(args.root)
    registry = SuiteRegistry(args.suite_root)
    result = asyncio.run(execute_run(args.run_id, registry=registry, store=store))
    return 0 if result.status is EvalRunStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
