"""Tests for read-only Redis Cluster diagnostics."""

import inspect

import pytest

from redis_sre_agent.core.instances import RedisInstance
from redis_sre_agent.tools.diagnostics.redis_command.provider import RedisCommandToolProvider


def _provider() -> RedisCommandToolProvider:
    instance = RedisInstance(
        id="cluster-1",
        name="orders-cluster",
        connection_url="redis://cluster.example:6379",
        environment="test",
        usage="cache",
        description="Test Cluster",
        instance_type="oss_cluster",
    )
    return RedisCommandToolProvider(redis_instance=instance)


class ClusterClient:
    async def execute_command(self, *command):
        if command == ("CLUSTER", "NODES"):
            return (
                "node-1 10.0.0.1:6379@16379 master - 0 0 1 connected 0-8191\n"
                "node-2 10.0.0.2:6379@16379 slave node-1 0 0 2 connected\n"
            )
        if command == ("CLUSTER", "SLOTS"):
            return [
                [0, 8191, ["10.0.0.1", 6379, "node-1"], ["10.0.0.2", 6379, "node-2"]]
            ]
        raise AssertionError(f"Unexpected command: {command}")


def test_cluster_tool_schemas_include_topology_and_replication_tools() -> None:
    provider = _provider()
    names = {schema.name for schema in provider.create_tool_schemas()}

    for operation in ("cluster_info", "cluster_nodes", "cluster_slots", "replication_info"):
        assert any(name.endswith(f"_{operation}") for name in names)


@pytest.mark.asyncio
async def test_cluster_nodes_returns_stable_structured_output() -> None:
    provider = _provider()
    provider._client = ClusterClient()

    result = await provider.cluster_nodes()

    assert result == {
        "status": "success",
        "nodes": [
            {
                "node_id": "node-1",
                "address": "10.0.0.1:6379@16379",
                "flags": ["master"],
                "master_id": None,
                "config_epoch": 1,
                "link_state": "connected",
                "slots": ["0-8191"],
            },
            {
                "node_id": "node-2",
                "address": "10.0.0.2:6379@16379",
                "flags": ["slave"],
                "master_id": "node-1",
                "config_epoch": 2,
                "link_state": "connected",
                "slots": [],
            },
        ],
    }


@pytest.mark.asyncio
async def test_cluster_slots_returns_primary_and_replica_roles() -> None:
    provider = _provider()
    provider._client = ClusterClient()

    result = await provider.cluster_slots()

    assert result == {
        "status": "success",
        "slots": [
            {
                "start": 0,
                "end": 8191,
                "primary": {"host": "10.0.0.1", "port": 6379, "node_id": "node-1"},
                "replicas": [{"host": "10.0.0.2", "port": 6379, "node_id": "node-2"}],
            }
        ],
    }


@pytest.mark.asyncio
async def test_cluster_readiness_uses_cluster_nodes_slots_and_replication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()

    async def cluster_info():
        return {"status": "success", "cluster_info": {"cluster_state": "ok"}}

    async def cluster_nodes():
        return {
            "status": "success",
            "nodes": [
                {"node_id": "primary", "flags": ["master"], "slots": ["0-8191"]},
                {"node_id": "replica", "flags": ["slave"], "slots": []},
            ],
        }

    async def cluster_slots():
        return {"status": "success", "slots": [{"start": 0, "end": 8191}]}

    async def replication_info():
        return {"status": "success", "role": {"type": "master"}}

    monkeypatch.setattr(provider, "cluster_info", cluster_info)
    monkeypatch.setattr(provider, "cluster_nodes", cluster_nodes)
    monkeypatch.setattr(provider, "cluster_slots", cluster_slots)
    monkeypatch.setattr(provider, "replication_info", replication_info)

    result = await provider.cluster_readiness()

    assert result["status"] == "success"
    assert result["summary"] == {
        "cluster_state": "ok",
        "primary_nodes": 1,
        "replica_nodes": 1,
        "failed_nodes": [],
        "slot_ranges": 1,
    }


def test_standalone_cluster_command_errors_do_not_prevent_provider_initialization() -> None:
    provider = _provider()

    assert provider.tools()


def test_cluster_diagnostics_do_not_include_cluster_mutation_commands() -> None:
    source = inspect.getsource(RedisCommandToolProvider)

    for command in ("CLUSTER FAILOVER", "CLUSTER RESET", "CLUSTER MEET", "CLUSTER FORGET", "CLUSTER SETSLOT"):
        assert command not in source
