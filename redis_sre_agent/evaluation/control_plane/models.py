"""Persisted contracts for the eval control plane."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from redis_sre_agent.evaluation.live_suite import LiveEvalComparisonSummary
from redis_sre_agent.evaluation.report_schema import EvalBaselinePolicy
from redis_sre_agent.evaluation.scenarios import ExecutionLane


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvalRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


ACTIVE_RUN_STATUSES = {EvalRunStatus.QUEUED, EvalRunStatus.RUNNING}


class SuiteScenarioDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None = None
    lane: ExecutionLane


class SuiteDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None = None
    manifest: str
    config_path: str = Field(exclude=True)
    digest: str
    scenario_count: int = Field(ge=1)
    scenarios: list[SuiteScenarioDescriptor]
    judge_pass_threshold: float | None = None
    baseline_profile: str | None = None
    baseline_policy: EvalBaselinePolicy = Field(default_factory=EvalBaselinePolicy)


class SuiteDiscoveryError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: str
    error: str


class SuiteDiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suites: list[SuiteDescriptor] = Field(default_factory=list)
    errors: list[SuiteDiscoveryError] = Field(default_factory=list)


class EffectiveEvalConfig(BaseModel):
    """Non-secret settings that make a run attributable and reproducible."""

    model_config = ConfigDict(extra="forbid")

    agent_models: dict[str, str] = Field(default_factory=dict)
    judge_model: str | None = None
    llm_factory: str | None = None
    base_url_origin: str | None = None
    redis_image: str | None = None
    baseline_profile: str | None = None
    judge_pass_threshold: float | None = None


class EvalRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    run_id: str
    suite_id: str
    suite_name: str
    suite_manifest: str
    suite_digest: str
    scenario_ids: list[str] = Field(default_factory=list)
    is_partial: bool = False
    status: EvalRunStatus = EvalRunStatus.QUEUED
    created_at: str = Field(default_factory=utc_now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    requested_by: str = "local"
    git_sha: str
    git_dirty: bool = False
    git_diff_digest: str | None = None
    effective_config: EffectiveEvalConfig = Field(default_factory=EffectiveEvalConfig)
    baseline_policy_snapshot: EvalBaselinePolicy = Field(default_factory=EvalBaselinePolicy)
    report_root: str = "reports"
    summary_path: str | None = None
    total_scenarios: int = 0
    passed_scenarios: int = 0
    failed_scenarios: int = 0
    pass_rate: float | None = Field(default=None, ge=0, le=1)
    judge_score: float | None = None
    evaluation_passed: bool | None = None
    error: str | None = None
    worker_pid: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunStoreError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    error: str


class EvalRunPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[EvalRunRecord] = Field(default_factory=list)
    next_cursor: str | None = None
    errors: list[RunStoreError] = Field(default_factory=list)


class EvalRunComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_run_id: str
    candidate_run_id: str
    suite_id: str
    policy: EvalBaselinePolicy
    summary: LiveEvalComparisonSummary


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "EffectiveEvalConfig",
    "EvalRunPage",
    "EvalRunComparison",
    "EvalRunRecord",
    "EvalRunStatus",
    "RunStoreError",
    "SuiteDescriptor",
    "SuiteDiscoveryError",
    "SuiteDiscoveryResult",
    "SuiteScenarioDescriptor",
    "utc_now_iso",
]
