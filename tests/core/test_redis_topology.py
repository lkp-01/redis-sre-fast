"""Tests for deterministic Redis protocol topology probing."""

import asyncio
import ssl

import pytest
from redis.exceptions import NoPermissionError

from redis_sre_agent.core.redis_topology import (
    RedisTopologyProbeError,
    classify_redis_endpoint,
    probe_redis_topology,
)


class FakeRedisClient:
    def __init__(self, *, info_result=None, info_error=None, cluster_result=None, cluster_error=None):
        self.info_result = info_result
        self.info_error = info_error
        self.cluster_result = cluster_result
        self.cluster_error = cluster_error
        self.closed = False

    async def info(self):
        if self.info_error:
            raise self.info_error
        return self.info_result

    async def execute_command(self, *command):
        assert command == ("CLUSTER", "INFO")
        if self.cluster_error:
            raise self.cluster_error
        return self.cluster_result

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cluster_enabled", "expected_type"),
    [(0, "oss_single"), (1, "oss_cluster")],
)
async def test_probe_classifies_info_cluster_enabled(
    monkeypatch: pytest.MonkeyPatch, cluster_enabled: int, expected_type: str
) -> None:
    client = FakeRedisClient(info_result={"cluster_enabled": cluster_enabled})
    monkeypatch.setattr(
        "redis_sre_agent.core.redis_topology.Redis.from_url", lambda *args, **kwargs: client
    )

    result = await probe_redis_topology("redis://user:secret@target.example:6379/0")

    assert result.instance_type.value == expected_type
    assert result.error is None
    assert result.evidence == (f"INFO cluster_enabled={cluster_enabled}",)
    assert client.closed


@pytest.mark.asyncio
async def test_probe_uses_cluster_info_when_info_lacks_cluster_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedisClient(
        info_result={"redis_version": "7.4.0"},
        cluster_result="cluster_state:ok\r\ncluster_slots_assigned:16384\r\n",
    )
    monkeypatch.setattr(
        "redis_sre_agent.core.redis_topology.Redis.from_url", lambda *args, **kwargs: client
    )

    result = await probe_redis_topology("redis://target.example:6379")

    assert result.instance_type.value == "oss_cluster"
    assert result.evidence == ("INFO cluster_enabled missing", "CLUSTER INFO cluster_state=ok")
    assert result.error is None
    assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (NoPermissionError("NOPERM this user has no permissions"), RedisTopologyProbeError.permission),
        (asyncio.TimeoutError(), RedisTopologyProbeError.timeout),
        (ssl.SSLError("TLS handshake failed"), RedisTopologyProbeError.tls),
    ],
)
async def test_probe_returns_safe_structured_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected_error: RedisTopologyProbeError
) -> None:
    client = FakeRedisClient(info_error=error)
    monkeypatch.setattr(
        "redis_sre_agent.core.redis_topology.Redis.from_url", lambda *args, **kwargs: client
    )

    result = await probe_redis_topology("redis://user:secret@target.example:6379")

    assert result.instance_type is None
    assert result.error == expected_error
    assert "secret" not in result.error_summary
    assert client.closed


@pytest.mark.asyncio
async def test_probe_returns_protocol_uncertain_when_cluster_info_cannot_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedisClient(
        info_result={"redis_version": "7.4.0"},
        cluster_result="cluster_slots_assigned:16384\r\n",
    )
    monkeypatch.setattr(
        "redis_sre_agent.core.redis_topology.Redis.from_url", lambda *args, **kwargs: client
    )

    result = await probe_redis_topology("redis://target.example:6379")

    assert result.instance_type is None
    assert result.error == RedisTopologyProbeError.protocol_uncertain
    assert client.closed


@pytest.mark.asyncio
async def test_classification_rejects_an_explicit_type_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedisClient(info_result={"cluster_enabled": 0})
    monkeypatch.setattr(
        "redis_sre_agent.core.redis_topology.Redis.from_url", lambda *args, **kwargs: client
    )

    with pytest.raises(ValueError, match="does not match"):
        await classify_redis_endpoint("redis://target.example:6379", "oss_cluster")
