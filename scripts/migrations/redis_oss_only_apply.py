"""Guarded OSS-only migration entry point; destructive application needs an approved backup."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path

from redis.asyncio import Redis

from redis_sre_agent.core.config import settings
from redis_sre_agent.core.targets import sync_target_catalog_from_authoritative_records


def validate_apply_request(report_path: Path, backup_path: Path, confirmed: bool) -> dict:
    if not confirmed:
        raise ValueError("Refusing migration without --confirm.")
    if not backup_path.parent.exists() or backup_path.exists():
        raise ValueError("Refusing migration: backup path must be in an existing empty location.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("mode") != "dry-run":
        raise ValueError("Refusing migration: report is not a dry-run preflight report.")
    return report


def _backup_value(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


async def apply_migration(report_path: Path, backup_path: Path, confirmed: bool) -> dict:
    """Back up and delete only records explicitly identified by raw preflight."""
    report = validate_apply_request(report_path, backup_path, confirmed)
    findings = report.get("blocking_findings") or []
    keys = sorted({str(finding["key"]) for finding in findings if finding.get("key")})
    client = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=False)
    try:
        backup: dict[str, dict[str, str]] = {}
        for key in keys:
            values = await client.hgetall(key)
            if values:
                backup[key] = {
                    _backup_value(field): _backup_value(value) for field, value in values.items()
                }
            else:
                value = await client.get(key)
                if value is not None:
                    backup[key] = {"__string__": _backup_value(value)}
        backup_path.write_text(json.dumps({"format": "base64-redis-v1", "records": backup}, indent=2), encoding="utf-8")

        deleted = 0
        for key in keys:
            if key.startswith(("sre_instances:", "sre_clusters:", "sre_targets:")):
                deleted += await client.delete(key)

        await sync_target_catalog_from_authoritative_records()
        return {"backup": str(backup_path), "deleted_keys": deleted, "scanned_findings": len(keys)}
    finally:
        await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Back up and apply the approved OSS-only data migration.")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(apply_migration(args.report, args.backup, args.confirm))
    print(json.dumps(result))
