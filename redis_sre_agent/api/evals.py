"""Authenticated API for suite discovery, historical eval runs, and comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from redis_sre_agent.evaluation.control_plane.comparison import (
    EvalComparisonService,
    InvalidEvalComparisonError,
)
from redis_sre_agent.evaluation.control_plane.manager import (
    ActiveEvalRunError,
    EvalRunManager,
)
from redis_sre_agent.evaluation.control_plane.models import (
    EvalRunComparison,
    EvalRunPage,
    EvalRunRecord,
    EvalRunStatus,
    SuiteDescriptor,
    SuiteDiscoveryResult,
)
from redis_sre_agent.evaluation.control_plane.registry import SuiteRegistry
from redis_sre_agent.evaluation.control_plane.store import FileEvalRunStore
from redis_sre_agent.evaluation.report_schema import EvalReportBundle

router = APIRouter(prefix="/api/v1/evals", tags=["evals"])


@dataclass
class EvalControlPlaneServices:
    registry: SuiteRegistry
    store: FileEvalRunStore
    manager: EvalRunManager
    comparison: EvalComparisonService


class CreateEvalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str = Field(min_length=1)


class CompareEvalRunsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_run_id: str
    candidate_run_id: str


class EvalReportListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    scenario_name: str | None = None
    overall_pass: bool | None = None
    judge_score: float | None = None


class EvalReportListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[EvalReportListItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def _services(request: Request) -> EvalControlPlaneServices:
    services = getattr(request.app.state, "eval_control_plane", None)
    if services is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "eval control plane is disabled or unavailable; set "
                "EVAL_CONTROL_ENABLED=true and restart the API process"
            ),
        )
    return services


def _get_run(store: FileEvalRunStore, run_id: str) -> EvalRunRecord:
    try:
        return store.get(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="eval run not found") from exc


def _run_report_root(store: FileEvalRunStore, record: EvalRunRecord) -> Path:
    run_dir = store.run_dir(record.run_id).resolve()
    report_root = (run_dir / record.report_root).resolve()
    if report_root != run_dir and run_dir not in report_root.parents:
        raise HTTPException(status_code=500, detail="invalid persisted report root")
    return report_root


def _load_reports(store: FileEvalRunStore, record: EvalRunRecord) -> tuple[list[EvalReportBundle], list[str]]:
    reports: list[EvalReportBundle] = []
    errors: list[str] = []
    for path in sorted(_run_report_root(store, record).rglob("report.json")):
        try:
            reports.append(EvalReportBundle.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValidationError, ValueError) as exc:
            errors.append(f"{path.relative_to(store.run_dir(record.run_id)).as_posix()}: {exc}")
    return reports, errors


@router.get("/suites", response_model=SuiteDiscoveryResult)
async def list_eval_suites(request: Request) -> SuiteDiscoveryResult:
    return _services(request).registry.discover()


@router.get("/suites/{suite_id}", response_model=SuiteDescriptor)
async def get_eval_suite(suite_id: str, request: Request) -> SuiteDescriptor:
    try:
        return _services(request).registry.get(suite_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="eval suite not found") from exc


@router.post("/runs", response_model=EvalRunRecord, status_code=status.HTTP_202_ACCEPTED)
async def create_eval_run(payload: CreateEvalRunRequest, request: Request) -> EvalRunRecord:
    services = _services(request)
    claims = getattr(request.state, "auth_claims", {}) or {}
    requested_by = str(claims.get("sub") or "local")
    try:
        return await services.manager.create_run(payload.suite_id, requested_by=requested_by)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="eval suite not found") from exc
    except ActiveEvalRunError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "another eval run is active", "active_run_id": exc.run_id},
        ) from exc


@router.get("/runs", response_model=EvalRunPage)
async def list_eval_runs(
    request: Request,
    suite_id: str | None = None,
    run_status: EvalRunStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> EvalRunPage:
    try:
        return _services(request).store.list(
            suite_id=suite_id,
            status=run_status,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/runs/{run_id}", response_model=EvalRunRecord)
async def get_eval_run(run_id: str, request: Request) -> EvalRunRecord:
    return _get_run(_services(request).store, run_id)


@router.get("/runs/{run_id}/reports", response_model=EvalReportListResponse)
async def list_eval_reports(run_id: str, request: Request) -> EvalReportListResponse:
    store = _services(request).store
    record = _get_run(store, run_id)
    reports, errors = _load_reports(store, record)
    return EvalReportListResponse(
        items=[
            EvalReportListItem(
                scenario_id=report.scenario_id,
                scenario_name=report.scenario_name,
                overall_pass=report.overall_pass,
                judge_score=(
                    report.judge_scores.overall_score if report.judge_scores is not None else None
                ),
            )
            for report in reports
        ],
        errors=errors,
    )


@router.get("/runs/{run_id}/report", response_model=dict[str, Any])
async def get_eval_report(
    run_id: str,
    request: Request,
    scenario_id: str = Query(min_length=1),
) -> dict[str, Any]:
    store = _services(request).store
    record = _get_run(store, run_id)
    reports, _errors = _load_reports(store, record)
    for report in reports:
        if report.scenario_id == scenario_id:
            return report.model_dump(mode="json")
    raise HTTPException(status_code=404, detail="eval scenario report not found")


@router.post("/comparisons", response_model=EvalRunComparison)
async def compare_eval_runs(
    payload: CompareEvalRunsRequest,
    request: Request,
) -> EvalRunComparison:
    try:
        return _services(request).comparison.compare(
            payload.baseline_run_id,
            payload.candidate_run_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="eval run not found") from exc
    except (InvalidEvalComparisonError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = ["EvalControlPlaneServices", "router"]
