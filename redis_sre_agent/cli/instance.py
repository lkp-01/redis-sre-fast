"""CLI commands for Redis protocol endpoint records."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

import click
from ulid import ULID

from redis_sre_agent.core import instances as core_instances
from redis_sre_agent.core.instance_mutation_helpers import _validate_instance_cluster_link
from redis_sre_agent.core.redis_topology import (
    RedisTopologyProbeResult,
    classify_redis_endpoint,
    probe_redis_topology,
)


def _payload(instance: core_instances.RedisInstance) -> dict:
    payload = instance.model_dump(mode="json")
    payload["connection_url"] = core_instances.mask_redis_url(instance.connection_url)
    payload.pop("extension_secrets", None)
    return payload


def _probe_payload(result: RedisTopologyProbeResult) -> dict:
    """Serialize a topology probe result at the CLI boundary."""
    return {
        "succeeded": result.succeeded,
        "instance_type": result.instance_type.value if result.instance_type else None,
        "evidence": list(result.evidence),
        "error": result.error.value if result.error else None,
        "error_summary": result.error_summary,
    }


def _emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
    else:
        click.echo(json.dumps(payload, indent=2, default=str))


@click.group()
def instance() -> None:
    """Manage standard Redis diagnostic endpoints."""


@instance.command("list")
@click.option("--instance-type", type=click.Choice(["oss_single", "oss_cluster"]))
@click.option("--json", "as_json", is_flag=True)
def list_instances(instance_type: Optional[str], as_json: bool) -> None:
    """List registered Redis endpoints."""

    async def run() -> None:
        result = await core_instances.query_instances(instance_type=instance_type)
        _emit([_payload(item) for item in result.instances], as_json=as_json)

    asyncio.run(run())


@instance.command("get")
@click.argument("instance_id")
@click.option("--json", "as_json", is_flag=True)
def get_instance(instance_id: str, as_json: bool) -> None:
    """Show one registered Redis endpoint."""

    async def run() -> None:
        item = await core_instances.get_instance_by_id(instance_id)
        if item is None:
            raise click.ClickException("Instance not found")
        _emit(_payload(item), as_json=as_json)

    asyncio.run(run())


@instance.command("create")
@click.option("--name", required=True)
@click.option("--connection-url", required=True, hide_input=True)
@click.option("--environment", type=click.Choice(["development", "staging", "production", "test"]), required=True)
@click.option("--usage", required=True)
@click.option("--description", required=True)
@click.option("--instance-type", type=click.Choice(["oss_single", "oss_cluster"]))
@click.option("--cluster-id")
@click.option("--user-id")
@click.option("--json", "as_json", is_flag=True)
def create_instance(
    name: str,
    connection_url: str,
    environment: str,
    usage: str,
    description: str,
    instance_type: Optional[str],
    cluster_id: Optional[str],
    user_id: Optional[str],
    as_json: bool,
) -> None:
    """Register an endpoint after Redis protocol probing."""

    async def run() -> None:
        items = await core_instances.get_instances()
        if any(item.name == name for item in items):
            raise click.ClickException(f"Instance with name '{name}' already exists")
        detected_type = await classify_redis_endpoint(connection_url, instance_type)
        linked_cluster_id = await _validate_instance_cluster_link(
            cluster_id=cluster_id, instance_type=detected_type.value
        )
        item = core_instances.RedisInstance(
            id=f"redis-{environment}-{ULID()}",
            name=name,
            connection_url=connection_url,
            environment=environment,
            usage=usage,
            description=description,
            instance_type=detected_type,
            cluster_id=linked_cluster_id,
            created_by="user",
            user_id=user_id,
        )
        if not await core_instances.save_instances([*items, item]):
            raise click.ClickException("Failed to save instance")
        _emit(_payload(item), as_json=as_json)

    asyncio.run(run())


@instance.command("update")
@click.argument("instance_id")
@click.option("--name")
@click.option("--connection-url", hide_input=True)
@click.option("--instance-type", type=click.Choice(["oss_single", "oss_cluster"]))
@click.option("--cluster-id")
@click.option("--description")
@click.option("--json", "as_json", is_flag=True)
def update_instance(
    instance_id: str,
    name: Optional[str],
    connection_url: Optional[str],
    instance_type: Optional[str],
    cluster_id: Optional[str],
    description: Optional[str],
    as_json: bool,
) -> None:
    """Update an endpoint and re-probe if connection details change."""

    async def run() -> None:
        items = await core_instances.get_instances()
        index = next((i for i, item in enumerate(items) if item.id == instance_id), None)
        if index is None:
            raise click.ClickException("Instance not found")
        current = items[index]
        changes: dict[str, object] = {}
        if name is not None:
            changes["name"] = name
        if description is not None:
            changes["description"] = description
        if connection_url is not None:
            changes["connection_url"] = connection_url
        if connection_url is not None or instance_type is not None:
            changes["instance_type"] = await classify_redis_endpoint(
                connection_url or current.connection_url.get_secret_value(),
                instance_type or current.instance_type,
            )
        if cluster_id is not None or "instance_type" in changes:
            changes["cluster_id"] = await _validate_instance_cluster_link(
                cluster_id=cluster_id if cluster_id is not None else current.cluster_id,
                instance_type=getattr(changes.get("instance_type", current.instance_type), "value", current.instance_type),
            )
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        item = core_instances.RedisInstance.model_validate(current.model_dump() | changes)
        items[index] = item
        if not await core_instances.save_instances(items):
            raise click.ClickException("Failed to save instance")
        _emit(_payload(item), as_json=as_json)

    asyncio.run(run())


@instance.command("delete")
@click.argument("instance_id")
@click.option("--yes", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def delete_instance(instance_id: str, yes: bool, as_json: bool) -> None:
    """Delete an endpoint."""
    if not yes:
        raise click.ClickException("Deletion requires --yes")

    async def run() -> None:
        items = await core_instances.get_instances()
        remaining = [item for item in items if item.id != instance_id]
        if len(remaining) == len(items):
            raise click.ClickException("Instance not found")
        if not await core_instances.save_instances(remaining):
            raise click.ClickException("Failed to save instance changes")
        await core_instances.delete_instance_index_doc(instance_id)
        _emit({"id": instance_id, "status": "deleted"}, as_json=as_json)

    asyncio.run(run())


@instance.command("test-url")
@click.option("--connection-url", required=True, hide_input=True)
@click.option("--json", "as_json", is_flag=True)
def test_url(connection_url: str, as_json: bool) -> None:
    """Probe a Redis URL without registering it."""
    result = asyncio.run(probe_redis_topology(connection_url))
    _emit(_probe_payload(result), as_json=as_json)


@instance.command("test")
@click.argument("instance_id")
@click.option("--json", "as_json", is_flag=True)
def test_instance(instance_id: str, as_json: bool) -> None:
    """Probe a registered endpoint."""

    async def run() -> None:
        item = await core_instances.get_instance_by_id(instance_id)
        if item is None:
            raise click.ClickException("Instance not found")
        result = await probe_redis_topology(item.connection_url.get_secret_value())
        _emit(_probe_payload(result), as_json=as_json)

    asyncio.run(run())
