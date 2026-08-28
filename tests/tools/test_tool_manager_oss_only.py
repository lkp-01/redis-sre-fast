"""ToolManager behaviour for protocol-classified OSS targets."""

import pytest

from redis_sre_agent.core.clusters import RedisCluster
from redis_sre_agent.core.instances import RedisInstance
from redis_sre_agent.tools.manager import ToolManager


@pytest.mark.asyncio
async def test_oss_cluster_loads_the_standard_redis_command_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = RedisInstance(
        id="cluster-1",
        name="orders-cluster",
        connection_url="redis://cluster.example:6379",
        environment="test",
        usage="cache",
        description="Test Cluster",
        instance_type="oss_cluster",
    )
    manager = ToolManager(redis_instance=instance)
    loaded: list[str] = []

    async def record_provider(provider_path: str, **kwargs) -> None:
        loaded.append(provider_path)

    monkeypatch.setattr(manager, "_load_provider", record_provider)
    monkeypatch.setattr(
        "redis_sre_agent.core.config.settings.tool_providers",
        ["redis_sre_agent.tools.diagnostics.redis_command.provider.RedisCommandToolProvider"],
    )

    await manager._load_instance_scoped_providers(instance)

    assert loaded == ["redis_sre_agent.tools.diagnostics.redis_command.provider.RedisCommandToolProvider"]


@pytest.mark.asyncio
async def test_cluster_metadata_never_loads_a_diagnostic_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = RedisCluster(
        id="cluster-1",
        name="orders-cluster",
        cluster_type="oss_cluster",
        environment="test",
        description="Test Cluster",
    )
    manager = ToolManager(redis_cluster=cluster)

    async def fail_if_loaded(*args, **kwargs) -> None:
        raise AssertionError("Cluster metadata must not load a diagnostic provider")

    monkeypatch.setattr(manager, "_load_provider", fail_if_loaded)

    await manager._load_cluster_scoped_providers(cluster)
