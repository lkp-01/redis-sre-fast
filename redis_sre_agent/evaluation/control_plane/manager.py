"""Single-active-run orchestration and isolated worker process management."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import psutil
from ulid import ULID

from redis_sre_agent.core.config import settings

from .models import (
    ACTIVE_RUN_STATUSES,
    EffectiveEvalConfig,
    EvalRunRecord,
    EvalRunStatus,
    SuiteDescriptor,
    utc_now_iso,
)
from .registry import SuiteRegistry
from .store import FileEvalRunStore

_SECRET_TOKEN_RE = re.compile(r"(?i)(api[_-]?key|authorization)(\s*[:=]\s*)[^\s,;]+")
_URL_CREDENTIAL_RE = re.compile(r"(://)[^/@\s]+@")
logger = logging.getLogger(__name__)


class ActiveEvalRunError(RuntimeError):
    def __init__(self, run_id: str | None) -> None:
        self.run_id = run_id
        super().__init__(f"an eval run is already active: {run_id or 'unknown'}")


class InvalidScenarioSelectionError(ValueError):
    def __init__(
        self,
        *,
        suite_id: str,
        unknown_ids: list[str] | None = None,
        duplicate_ids: list[str] | None = None,
        empty: bool = False,
    ) -> None:
        self.suite_id = suite_id
        self.unknown_ids = unknown_ids or []
        self.duplicate_ids = duplicate_ids or []
        if empty:
            message = "scenario_ids must contain at least one scenario"
        elif self.unknown_ids:
            message = f"unknown scenario ids for suite '{suite_id}': {', '.join(self.unknown_ids)}"
        else:
            message = f"duplicate scenario ids: {', '.join(self.duplicate_ids)}"
        super().__init__(message)


def _select_scenario_ids(
    suite: SuiteDescriptor,
    scenario_ids: list[str] | None,
) -> list[str]:
    available_ids = [scenario.id for scenario in suite.scenarios]
    if scenario_ids is None:
        return available_ids
    if not scenario_ids:
        raise InvalidScenarioSelectionError(suite_id=suite.id, empty=True)

    seen: set[str] = set()
    duplicate_ids: list[str] = []
    for scenario_id in scenario_ids:
        if scenario_id in seen and scenario_id not in duplicate_ids:
            duplicate_ids.append(scenario_id)
        seen.add(scenario_id)
    if duplicate_ids:
        raise InvalidScenarioSelectionError(
            suite_id=suite.id,
            duplicate_ids=duplicate_ids,
        )

    available_set = set(available_ids)
    unknown_ids = [scenario_id for scenario_id in scenario_ids if scenario_id not in available_set]
    if unknown_ids:
        raise InvalidScenarioSelectionError(
            suite_id=suite.id,
            unknown_ids=unknown_ids,
        )
    requested_set = set(scenario_ids)
    return [scenario_id for scenario_id in available_ids if scenario_id in requested_set]


def sanitize_run_error(error: object) -> str:
    text = " ".join(str(error).split())
    text = _SECRET_TOKEN_RE.sub(r"\1\2***", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1***@", text)
    return text[:2000]


def describe_run_error(error: object) -> str:
    """Return a safe, useful error even for exceptions with an empty message."""

    return sanitize_run_error(error) or type(error).__name__


def _base_url_origin(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    return None


def capture_effective_config(*, baseline_profile: str | None, threshold: float | None) -> EffectiveEvalConfig:
    return EffectiveEvalConfig(
        agent_models={
            "main": settings.openai_model,
            "mini": settings.openai_model_mini,
            "nano": settings.openai_model_nano,
        },
        judge_model=settings.openai_model_mini,
        llm_factory=settings.llm_factory,
        base_url_origin=_base_url_origin(settings.openai_base_url),
        redis_image=os.getenv("EVAL_REDIS_IMAGE", "redis:8"),
        baseline_profile=baseline_profile,
        judge_pass_threshold=threshold,
    )


def capture_git_state(repo_root: Path) -> tuple[str, bool, str | None]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
    )
    sha = revision.stdout.strip() if revision.returncode == 0 else "unknown"
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        return sha, dirty, None
    digest = hashlib.sha256(diff.stdout + b"\0" + status.stdout).hexdigest()
    return sha, dirty, digest


def _is_live_worker(pid: int, run_id: str) -> bool:
    try:
        command = " ".join(psutil.Process(pid).cmdline())
    except (psutil.Error, OSError):
        return False
    return "redis_sre_agent.evaluation.control_plane.worker" in command and run_id in command


class EvalRunManager:
    def __init__(
        self,
        registry: SuiteRegistry,
        store: FileEvalRunStore,
        *,
        repo_root: str | Path,
        process_launcher: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.lock_path = self.store.root / "active.lock"
        self._process_launcher = process_launcher or self._spawn_worker
        self._tasks: set[asyncio.Task[None]] = set()

    def _read_lock(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _acquire_lock(self, run_id: str) -> None:
        payload = json.dumps({"run_id": run_id, "pid": None, "created_at": utc_now_iso()})
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing = self._read_lock() or {}
            raise ActiveEvalRunError(str(existing.get("run_id") or "") or None) from exc
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)

    def update_lock_pid(self, run_id: str, pid: int) -> None:
        payload = self._read_lock()
        if not payload or payload.get("run_id") != run_id:
            raise RuntimeError("active eval lock is missing or owned by another run")
        payload["pid"] = pid
        temporary = self.lock_path.with_name(f"active.lock.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, self.lock_path)

    def release_lock(self, run_id: str) -> None:
        payload = self._read_lock()
        if payload and payload.get("run_id") == run_id:
            self.lock_path.unlink(missing_ok=True)

    async def create_run(
        self,
        suite_id: str,
        *,
        requested_by: str = "local",
        scenario_ids: list[str] | None = None,
    ) -> EvalRunRecord:
        suite = self.registry.get(suite_id)
        selected_scenario_ids = _select_scenario_ids(suite, scenario_ids)
        run_id = str(ULID())
        self._acquire_lock(run_id)
        try:
            git_sha, git_dirty, git_diff_digest = capture_git_state(self.repo_root)
            record = EvalRunRecord(
                run_id=run_id,
                suite_id=suite.id,
                suite_name=suite.name,
                suite_manifest=suite.manifest,
                suite_digest=suite.digest,
                scenario_ids=selected_scenario_ids,
                is_partial=len(selected_scenario_ids) < len(suite.scenarios),
                requested_by=requested_by,
                git_sha=git_sha,
                git_dirty=git_dirty,
                git_diff_digest=git_diff_digest,
                effective_config=capture_effective_config(
                    baseline_profile=suite.baseline_profile,
                    threshold=suite.judge_pass_threshold,
                ),
                baseline_policy_snapshot=suite.baseline_policy,
            )
            self.store.save(record)
        except Exception:
            self.release_lock(run_id)
            raise

        task = asyncio.create_task(self._launch_and_guard(run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def _launch_and_guard(self, run_id: str) -> None:
        try:
            await self._process_launcher(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                record = self.store.get(run_id)
            except (FileNotFoundError, ValueError):
                self.release_lock(run_id)
                return
            if record.status in ACTIVE_RUN_STATUSES:
                record.status = EvalRunStatus.FAILED
                record.completed_at = utc_now_iso()
                record.error = describe_run_error(exc)
                self.store.save(record)
            logger.error("eval worker launch failed run_id=%s error=%s", run_id, describe_run_error(exc))
            self.release_lock(run_id)

    async def _spawn_worker(self, run_id: str) -> None:
        run_dir = self.store.run_dir(run_id)
        stdout_path = run_dir / "worker.stdout.log"
        stderr_path = run_dir / "worker.stderr.log"
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            # Uvicorn may use a Windows selector event loop in reload mode, where
            # asyncio subprocess APIs raise NotImplementedError. Popen works with
            # every supported event loop; wait in a worker thread to keep the API
            # request loop responsive.
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "redis_sre_agent.evaluation.control_plane.worker",
                    "--root",
                    str(self.store.root),
                    "--suite-root",
                    str(self.registry.root),
                    "--run-id",
                    run_id,
                ],
                cwd=str(self.repo_root),
                stdout=stdout,
                stderr=stderr,
            )
            self.update_lock_pid(run_id, process.pid)
            return_code = await asyncio.to_thread(process.wait)
        if return_code == 0:
            return
        try:
            record = self.store.get(run_id)
        except (FileNotFoundError, ValueError):
            self.release_lock(run_id)
            return
        if record.status in ACTIVE_RUN_STATUSES:
            record.status = EvalRunStatus.FAILED
            record.completed_at = utc_now_iso()
            record.error = f"eval worker exited with code {return_code}"
            self.store.save(record)
            self.release_lock(run_id)

    async def recover(self) -> None:
        payload = self._read_lock()
        if payload is None and self.lock_path.exists():
            self.lock_path.unlink(missing_ok=True)
        locked_run_id = str(payload.get("run_id")) if payload and payload.get("run_id") else None
        if locked_run_id:
            try:
                record = self.store.get(locked_run_id)
            except (FileNotFoundError, ValueError):
                self.lock_path.unlink(missing_ok=True)
            else:
                pid = record.worker_pid or int(payload.get("pid") or 0)
                if record.status in ACTIVE_RUN_STATUSES and pid and _is_live_worker(pid, record.run_id):
                    return
                if record.status in ACTIVE_RUN_STATUSES:
                    record.status = EvalRunStatus.INTERRUPTED
                    record.completed_at = utc_now_iso()
                    record.error = "eval worker was not running during control-plane recovery"
                    self.store.save(record)
                self.release_lock(locked_run_id)

        page = self.store.list(limit=200)
        for record in page.items:
            if record.status in ACTIVE_RUN_STATUSES:
                record.status = EvalRunStatus.INTERRUPTED
                record.completed_at = utc_now_iso()
                record.error = "active run had no live control-plane lock during recovery"
                self.store.save(record)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


__all__ = [
    "ActiveEvalRunError",
    "EvalRunManager",
    "InvalidScenarioSelectionError",
    "capture_effective_config",
    "capture_git_state",
    "describe_run_error",
    "sanitize_run_error",
]
