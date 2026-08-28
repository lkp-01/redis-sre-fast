"""Persistence boundary tests for Redis Cluster metadata."""

import pytest

from redis_sre_agent.core.clusters import RedisCluster, save_clusters


def _cluster(cluster_type: str) -> RedisCluster:
    return RedisCluster(
        id="cluster-1",
        name="orders-cluster",
        cluster_type=cluster_type,
        environment="test",
        description="Test cluster",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cluster_type", ["unknown"])
async def test_save_clusters_rejects_non_oss_cluster_types(cluster_type: str) -> None:
    with pytest.raises(ValueError, match="only supports oss_cluster"):
        await save_clusters([_cluster(cluster_type)])


@pytest.mark.asyncio
async def test_save_clusters_allows_oss_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    persisted: list[RedisCluster] = []

    async def record_clusters(clusters: list[RedisCluster]) -> bool:
        persisted.extend(clusters)
        return True

    monkeypatch.setattr("redis_sre_agent.core.clusters._save_clusters", record_clusters)

    assert await save_clusters([_cluster("oss_cluster")])
    assert [cluster.cluster_type.value for cluster in persisted] == ["oss_cluster"]
