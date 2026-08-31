"""CLI coverage for Redis endpoint probe serialization."""

import json
from types import SimpleNamespace

from click.testing import CliRunner
from pydantic import SecretStr

from redis_sre_agent.cli.instance import instance
from redis_sre_agent.core.instances import RedisInstanceType
from redis_sre_agent.core.redis_topology import RedisTopologyProbeResult


async def _successful_probe(_connection_url: str) -> RedisTopologyProbeResult:
    return RedisTopologyProbeResult(
        instance_type=RedisInstanceType.oss_single,
        evidence=("INFO cluster_enabled=0",),
    )


def _assert_success_payload(output: str) -> None:
    payload = json.loads(output)
    assert payload == {
        "succeeded": True,
        "instance_type": "oss_single",
        "evidence": ["INFO cluster_enabled=0"],
        "error": None,
        "error_summary": None,
    }


def test_test_url_serializes_dataclass_probe_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "redis_sre_agent.cli.instance.probe_redis_topology",
        _successful_probe,
    )

    result = CliRunner().invoke(
        instance,
        ["test-url", "--connection-url", "redis://target.example:6379/0", "--json"],
    )

    assert result.exit_code == 0, result.output
    _assert_success_payload(result.output)


def test_registered_instance_probe_serializes_dataclass_result(monkeypatch) -> None:
    async def get_instance_by_id(_instance_id: str):
        return SimpleNamespace(connection_url=SecretStr("redis://target.example:6379/0"))

    monkeypatch.setattr(
        "redis_sre_agent.cli.instance.core_instances.get_instance_by_id",
        get_instance_by_id,
    )
    monkeypatch.setattr(
        "redis_sre_agent.cli.instance.probe_redis_topology",
        _successful_probe,
    )

    result = CliRunner().invoke(instance, ["test", "redis-test-example", "--json"])

    assert result.exit_code == 0, result.output
    _assert_success_payload(result.output)
