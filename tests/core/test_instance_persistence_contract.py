"""Persistence boundary tests for Redis instances."""

import pytest

from redis_sre_agent.core.instances import RedisInstance, save_instances


def _instance(instance_type: str) -> RedisInstance:
    return RedisInstance(
        id="instance-1",
        name="orders",
        connection_url="redis://target.example:6379",
        environment="test",
        usage="cache",
        description="Test target",
        instance_type=instance_type,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("instance_type", ["unknown"])
async def test_save_instances_rejects_non_oss_target_types(instance_type: str) -> None:
    with pytest.raises(ValueError, match="only supports oss_single and oss_cluster"):
        await save_instances([_instance(instance_type)])


@pytest.mark.asyncio
async def test_save_instances_allows_oss_target_types(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[RedisInstance] = []

    async def record_instances(instances: list[RedisInstance]) -> bool:
        persisted.extend(instances)
        return True

    monkeypatch.setattr("redis_sre_agent.core.instances._save_instances", record_instances)

    assert await save_instances([_instance("oss_single"), _instance("oss_cluster")])
    assert [instance.instance_type.value for instance in persisted] == ["oss_single", "oss_cluster"]
