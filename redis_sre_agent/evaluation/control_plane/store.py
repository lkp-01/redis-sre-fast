"""Atomic file-backed persistence for eval run manifests."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import ValidationError

from .models import EvalRunPage, EvalRunRecord, EvalRunStatus, RunStoreError

_RUN_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class FileEvalRunStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        normalized = str(run_id).strip().upper()
        if not _RUN_ID_RE.fullmatch(normalized):
            raise ValueError(f"invalid run id: {run_id}")
        return normalized

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / self._validate_run_id(run_id)

    def save(self, record: EvalRunRecord) -> Path:
        run_dir = self.run_dir(record.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "run.json"
        temporary = run_dir / f"run.json.{os.getpid()}.tmp"
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, target)
        return target

    def get(self, run_id: str) -> EvalRunRecord:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            raise FileNotFoundError(run_id)
        return EvalRunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(
        self,
        *,
        suite_id: str | None = None,
        status: EvalRunStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> EvalRunPage:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        normalized_cursor = self._validate_run_id(cursor) if cursor else None
        records: list[EvalRunRecord] = []
        errors: list[RunStoreError] = []
        for path in sorted(self.runs_root.glob("*/run.json"), reverse=True):
            run_id = path.parent.name
            try:
                record = EvalRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError, ValueError) as exc:
                errors.append(RunStoreError(run_id=run_id, error=str(exc)))
                continue
            if suite_id is not None and record.suite_id != suite_id:
                continue
            if status is not None and record.status is not status:
                continue
            records.append(record)

        records.sort(key=lambda item: item.run_id, reverse=True)
        if normalized_cursor:
            records = [record for record in records if record.run_id < normalized_cursor]
        page_items = records[:limit]
        next_cursor = page_items[-1].run_id if len(records) > limit else None
        return EvalRunPage(items=page_items, next_cursor=next_cursor, errors=errors)


__all__ = ["FileEvalRunStore"]
