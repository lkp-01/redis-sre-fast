"""Observability tests for malformed records left behind after migration."""

import json

import pytest

from redis_sre_agent.core import clusters, instances


class _SingleInvalidRecordIndex:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self._calls = 0

    async def query(self, _query):
        self._calls += 1
        if self._calls == 1:
            return 1
        return [{"data": json.dumps(self._payload)}]


@pytest.mark.asyncio
async def test_invalid_persisted_instance_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    index = _SingleInvalidRecordIndex(
        {
            "id": "legacy-instance",
            "name": "legacy",
            "connection_url": "redis://target.example:6379",
            "environment": "test",
            "usage": "cache",
            "description": "legacy record",
            "instance_type": "redis_cloud",
        }
    )
    monkeypatch.setattr(instances, "_persisted_instance_load_error_count", 0)

    async def no_index_setup() -> None:
        return None

    async def get_index() -> _SingleInvalidRecordIndex:
        return index

    monkeypatch.setattr(instances, "_ensure_instances_index_exists", no_index_setup)
    monkeypatch.setattr(instances, "get_instances_index", get_index)

    assert await instances._load_instances_from_index() == []
    assert instances.get_persisted_instance_load_error_count() == 1


@pytest.mark.asyncio
async def test_invalid_persisted_cluster_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    index = _SingleInvalidRecordIndex(
        {
            "id": "legacy-cluster",
            "name": "legacy",
            "environment": "test",
            "description": "legacy record",
            "cluster_type": "redis_enterprise",
        }
    )
    monkeypatch.setattr(clusters, "_persisted_cluster_load_error_count", 0)

    async def no_index_setup() -> None:
        return None

    async def get_index() -> _SingleInvalidRecordIndex:
        return index

    monkeypatch.setattr(clusters, "_ensure_clusters_index_exists", no_index_setup)
    monkeypatch.setattr(clusters, "get_clusters_index", get_index)

    assert await clusters._load_clusters_from_index() == []
    assert clusters.get_persisted_cluster_load_error_count() == 1
