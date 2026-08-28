"""Protocol-based classification for Redis diagnostic endpoints."""

from __future__ import annotations

import asyncio
import inspect
import re
import ssl
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from redis.asyncio import Redis
from redis.exceptions import AuthenticationError, ConnectionError, NoPermissionError, ResponseError

from .instances import RedisInstanceType


class RedisTopologyProbeError(str, Enum):
    """Safe error categories returned when endpoint classification is inconclusive."""

    connection = "connection"
    permission = "permission"
    protocol_uncertain = "protocol_uncertain"
    timeout = "timeout"
    tls = "tls"


@dataclass(frozen=True)
class RedisTopologyProbeResult:
    """The protocol evidence and safe result of probing one Redis endpoint."""

    instance_type: Optional[RedisInstanceType]
    evidence: tuple[str, ...]
    error: Optional[RedisTopologyProbeError] = None
    error_summary: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.instance_type is not None


class RedisTopologyProbeFailedError(ValueError):
    """Raised when a formal target cannot be classified by the Redis protocol."""


_REDIS_URL_PATTERN = re.compile(r"rediss?://[^\s'\"]+", re.IGNORECASE)


def _safe_error_summary(error: Exception) -> str:
    """Produce a diagnostic summary without leaking a Redis URL or its credentials."""
    rendered = _REDIS_URL_PATTERN.sub("redis://***", str(error)).strip()
    return rendered or error.__class__.__name__


def _classify_error(error: Exception) -> RedisTopologyProbeError:
    if isinstance(error, ssl.SSLError):
        return RedisTopologyProbeError.tls
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return RedisTopologyProbeError.timeout
    if isinstance(error, (AuthenticationError, NoPermissionError)):
        return RedisTopologyProbeError.permission
    if isinstance(error, ResponseError) and any(
        marker in str(error).upper() for marker in ("NOAUTH", "NOPERM", "AUTH")
    ):
        return RedisTopologyProbeError.permission
    if isinstance(error, ConnectionError):
        return RedisTopologyProbeError.connection
    return RedisTopologyProbeError.protocol_uncertain


def _string_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _cluster_state(cluster_info: Any) -> Optional[str]:
    if isinstance(cluster_info, dict):
        state = cluster_info.get("cluster_state")
        return _string_value(state).strip().lower() if state is not None else None

    for line in _string_value(cluster_info).splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "cluster_state":
            return value.strip().lower()
    return None


async def _close_client(client: Redis) -> None:
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def probe_redis_topology(connection_url: str) -> RedisTopologyProbeResult:
    """Classify an endpoint using only standard Redis protocol responses.

    The probe first reads ``INFO`` and trusts the server's ``cluster_enabled``
    flag. When that field is absent, it uses ``CLUSTER INFO`` as a cross-check.
    All inconclusive outcomes are explicit failures; this function never guesses
    from a hostname, port, name, description, or vendor metadata.
    """
    client = Redis.from_url(connection_url, decode_responses=True)
    evidence: list[str] = []
    try:
        info = await client.info()
        cluster_enabled = info.get("cluster_enabled") if isinstance(info, dict) else None
        if cluster_enabled is not None:
            normalized = _string_value(cluster_enabled).strip().lower()
            if normalized in {"1", "true", "yes"}:
                evidence.append("INFO cluster_enabled=1")
                return RedisTopologyProbeResult(RedisInstanceType.oss_cluster, tuple(evidence))
            if normalized in {"0", "false", "no"}:
                evidence.append("INFO cluster_enabled=0")
                return RedisTopologyProbeResult(RedisInstanceType.oss_single, tuple(evidence))
            evidence.append("INFO cluster_enabled unrecognized")
        else:
            evidence.append("INFO cluster_enabled missing")

        cluster_info = await client.execute_command("CLUSTER", "INFO")
        state = _cluster_state(cluster_info)
        if state is not None:
            evidence.append(f"CLUSTER INFO cluster_state={state}")
            return RedisTopologyProbeResult(RedisInstanceType.oss_cluster, tuple(evidence))

        evidence.append("CLUSTER INFO cluster_state missing")
        return RedisTopologyProbeResult(
            None,
            tuple(evidence),
            RedisTopologyProbeError.protocol_uncertain,
            "Redis protocol probe could not confirm standalone or Cluster topology.",
        )
    except Exception as error:
        return RedisTopologyProbeResult(
            None,
            tuple(evidence),
            _classify_error(error),
            _safe_error_summary(error),
        )
    finally:
        await _close_client(client)


async def classify_redis_endpoint(
    connection_url: str,
    requested_type: Optional[str | RedisInstanceType] = None,
) -> RedisInstanceType:
    """Probe an endpoint and enforce any user-provided OSS type assertion."""
    requested_value = (
        requested_type.value if isinstance(requested_type, RedisInstanceType) else requested_type
    )
    normalized_requested = str(requested_value or "unknown").strip().lower()
    if normalized_requested not in {"unknown", "oss_single", "oss_cluster"}:
        raise ValueError("Redis instance type must be oss_single or oss_cluster when specified.")

    result = await probe_redis_topology(connection_url)
    if not result.succeeded:
        summary = result.error_summary or "Redis protocol probe was inconclusive."
        raise RedisTopologyProbeFailedError(
            f"Could not classify Redis endpoint ({result.error.value if result.error else 'unknown'}): "
            f"{summary}"
        )

    assert result.instance_type is not None
    if normalized_requested != "unknown" and result.instance_type.value != normalized_requested:
        raise ValueError(
            "Specified Redis instance type does not match the Redis protocol probe: "
            f"expected {normalized_requested}, detected {result.instance_type.value}."
        )
    return result.instance_type
