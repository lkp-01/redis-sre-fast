"""Raw Redis preflight for the OSS-only migration; it never mutates Redis."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from redis.asyncio import Redis

from redis_sre_agent.core.config import settings

_LEGACY_TYPES = {"redis_enterprise", "redis_cloud", "unknown"}
_LEGACY_FIELDS = {"admin_url", "admin_username", "admin_password"}
_LEGACY_PREFIXES = ("redis_cloud_",)
_SECRET_MARKERS = ("password", "secret", "api_key", "token", "connection_url")
_SCAN_PATTERNS = ("sre_instances:*", "sre_clusters:*", "sre_targets:*", "sre:thread:*:instances", "sre:task:*:resume_state", "sre:task:*:metadata", "sre_schedules:*")


def _text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "***" if any(marker in key.lower() for marker in _SECRET_MARKERS) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def inspect_record(key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a redacted finding for legacy types, fields, or runtime references."""
    type_value = str(payload.get("instance_type") or payload.get("cluster_type") or "").lower()
    fields = sorted(
        key_name
        for key_name in payload
        if key_name in _LEGACY_FIELDS or key_name.startswith(_LEGACY_PREFIXES)
    )
    serialized = json.dumps(payload, default=str).lower()
    runtime_reference = any(marker in serialized for marker in ("redis_enterprise", "redis_cloud", "support_package"))
    if type_value not in _LEGACY_TYPES and not fields and not runtime_reference:
        return None
    return {"key": key, "type": type_value or None, "legacy_fields": fields, "payload": _redact(payload)}


async def build_report(client: Redis) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    scanned = 0
    seen: set[str] = set()
    for pattern in _SCAN_PATTERNS:
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor, match=pattern, count=200)
            for raw_key in keys:
                key = _text(raw_key)
                if key in seen:
                    continue
                seen.add(key)
                scanned += 1
                raw_data = await client.hget(key, "data") if key.startswith(("sre_instances:", "sre_clusters:", "sre_targets:")) else await client.get(key)
                if not raw_data:
                    continue
                try:
                    payload = json.loads(_text(raw_data))
                except (TypeError, ValueError):
                    continue
                records = payload if isinstance(payload, list) else [payload]
                for record in records:
                    if isinstance(record, dict) and (finding := inspect_record(key, record)):
                        findings.append(finding)
            if cursor == 0:
                break
    return {"mode": "dry-run", "scanned_keys": scanned, "blocking_findings": findings, "blocking_count": len(findings)}


async def _main(output: Path) -> None:
    client = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=False)
    try:
        report = await build_report(client)
    finally:
        await client.aclose()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a non-mutating OSS-only Redis data preflight.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.output))
