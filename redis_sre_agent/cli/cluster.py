"""CLI commands for Redis Cluster grouping metadata."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

import click
from ulid import ULID

from redis_sre_agent.core import clusters as core_clusters
from redis_sre_agent.core import instances as core_instances


async def _payload(cluster: core_clusters.RedisCluster) -> dict:
    payload = cluster.model_dump(mode="json")
    payload.pop("extension_secrets", None)
    instances = await core_instances.get_instances()
    linked = [item.id for item in instances if item.cluster_id == cluster.id]
    payload["associated_instance_ids"] = linked
    payload["diagnosable"] = bool(linked)
    return payload


def _emit(payload: object) -> None:
    click.echo(json.dumps(payload, indent=2, default=str))


@click.group()
def cluster() -> None:
    """Manage Redis Cluster grouping metadata."""


@cluster.command("list")
@click.option("--json", "as_json", is_flag=True)
def list_clusters(as_json: bool) -> None:
    """List Redis Cluster metadata records."""

    async def run() -> None:
        result = await core_clusters.query_clusters(cluster_type="oss_cluster")
        _emit([await _payload(item) for item in result.clusters])

    asyncio.run(run())


@cluster.command("get")
@click.argument("cluster_id")
@click.option("--json", "as_json", is_flag=True)
def get_cluster(cluster_id: str, as_json: bool) -> None:
    """Show a Redis Cluster metadata record."""

    async def run() -> None:
        item = await core_clusters.get_cluster_by_id(cluster_id)
        if item is None:
            raise click.ClickException("Cluster not found")
        _emit(await _payload(item))

    asyncio.run(run())


@cluster.command("create")
@click.option("--name", required=True)
@click.option("--environment", type=click.Choice(["development", "staging", "production", "test"]), required=True)
@click.option("--description", required=True)
@click.option("--notes")
@click.option("--user-id")
@click.option("--json", "as_json", is_flag=True)
def create_cluster(
    name: str,
    environment: str,
    description: str,
    notes: Optional[str],
    user_id: Optional[str],
    as_json: bool,
) -> None:
    """Create a Redis Cluster grouping record, not a diagnostic endpoint."""

    async def run() -> None:
        items = await core_clusters.get_clusters()
        if any(item.name == name for item in items):
            raise click.ClickException(f"Cluster with name '{name}' already exists")
        item = core_clusters.RedisCluster(
            id=f"cluster-{environment}-{ULID()}",
            name=name,
            cluster_type="oss_cluster",
            environment=environment,
            description=description,
            notes=notes,
            created_by="user",
            user_id=user_id,
        )
        if not await core_clusters.save_clusters([*items, item]):
            raise click.ClickException("Failed to save cluster")
        _emit(await _payload(item))

    asyncio.run(run())


@cluster.command("update")
@click.argument("cluster_id")
@click.option("--name")
@click.option("--description")
@click.option("--notes")
@click.option("--json", "as_json", is_flag=True)
def update_cluster(
    cluster_id: str,
    name: Optional[str],
    description: Optional[str],
    notes: Optional[str],
    as_json: bool,
) -> None:
    """Update Redis Cluster grouping metadata."""

    async def run() -> None:
        items = await core_clusters.get_clusters()
        index = next((i for i, item in enumerate(items) if item.id == cluster_id), None)
        if index is None:
            raise click.ClickException("Cluster not found")
        current = items[index]
        changes = {"updated_at": datetime.now(timezone.utc).isoformat()}
        if name is not None:
            changes["name"] = name
        if description is not None:
            changes["description"] = description
        if notes is not None:
            changes["notes"] = notes
        item = core_clusters.RedisCluster.model_validate(current.model_dump() | changes)
        items[index] = item
        if not await core_clusters.save_clusters(items):
            raise click.ClickException("Failed to save cluster")
        _emit(await _payload(item))

    asyncio.run(run())


@cluster.command("delete")
@click.argument("cluster_id")
@click.option("--yes", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def delete_cluster(cluster_id: str, yes: bool, as_json: bool) -> None:
    """Delete a Redis Cluster metadata record."""
    if not yes:
        raise click.ClickException("Deletion requires --yes")

    async def run() -> None:
        items = await core_clusters.get_clusters()
        remaining = [item for item in items if item.id != cluster_id]
        if len(remaining) == len(items):
            raise click.ClickException("Cluster not found")
        if not await core_clusters.save_clusters(remaining):
            raise click.ClickException("Failed to save cluster changes")
        await core_clusters.delete_cluster_index_doc(cluster_id)
        _emit({"id": cluster_id, "status": "deleted"})

    asyncio.run(run())
