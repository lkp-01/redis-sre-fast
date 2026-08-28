"""Shared helpers for MCP cluster tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ulid import ULID

from redis_sre_agent.core import clusters as core_clusters


def _mask_cluster_payload(cluster: core_clusters.RedisCluster) -> Dict[str, Any]:
    """Convert a cluster model to a JSON-safe metadata payload."""
    return cluster.model_dump(mode="json")


async def list_clusters_helper(
    *,
    environment: Optional[str] = None,
    status: Optional[str] = None,
    cluster_type: Optional[str] = None,
    user_id: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return filtered cluster results with masked credentials."""
    result = await core_clusters.query_clusters(
        environment=environment,
        status=status,
        cluster_type=cluster_type,
        user_id=user_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return {
        "clusters": [_mask_cluster_payload(cluster) for cluster in result.clusters],
        "total": result.total,
        "limit": result.limit,
        "offset": result.offset,
    }


async def get_cluster_helper(cluster_id: str) -> Dict[str, Any]:
    """Return a single cluster payload or a not-found error."""
    cluster = await core_clusters.get_cluster_by_id(cluster_id)
    if not cluster:
        return {"error": "Cluster not found", "id": cluster_id}
    return _mask_cluster_payload(cluster)


async def create_cluster_helper(
    *,
    name: str,
    environment: str,
    description: str,
    notes: Optional[str] = None,
    status: Optional[str] = None,
    version: Optional[str] = None,
    last_checked: Optional[str] = None,
    created_by: str = "user",
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new cluster record."""
    clusters = await core_clusters.get_clusters()
    if any(cluster.name == name for cluster in clusters):
        raise RuntimeError(f"Cluster with name '{name}' already exists")

    cluster_id = f"cluster-{environment.lower()}-{ULID()}"
    new_cluster = core_clusters.RedisCluster(
        id=cluster_id,
        name=name,
        cluster_type="oss_cluster",
        environment=environment.lower(),
        description=description,
        notes=notes,
        status=status,
        version=version,
        last_checked=last_checked,
        created_by=created_by.lower() if created_by else "user",
        user_id=user_id,
    )

    clusters.append(new_cluster)
    if not await core_clusters.save_clusters(clusters):
        raise RuntimeError("Failed to save cluster")

    return {"id": new_cluster.id, "status": "created"}


async def update_cluster_helper(
    cluster_id: str,
    *,
    name: Optional[str] = None,
    environment: Optional[str] = None,
    description: Optional[str] = None,
    notes: Optional[str] = None,
    status: Optional[str] = None,
    version: Optional[str] = None,
    last_checked: Optional[str] = None,
    created_by: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Update fields on an existing cluster."""
    clusters = await core_clusters.get_clusters()
    cluster_index = next(
        (i for i, cluster in enumerate(clusters) if cluster.id == cluster_id), None
    )
    if cluster_index is None:
        raise RuntimeError("Cluster not found")

    update_data: Dict[str, Any] = {}
    if name is not None:
        update_data["name"] = name
    if environment is not None:
        update_data["environment"] = environment.lower()
    if description is not None:
        update_data["description"] = description
    if notes is not None:
        update_data["notes"] = notes
    if status is not None:
        update_data["status"] = status
    if version is not None:
        update_data["version"] = version
    if last_checked is not None:
        update_data["last_checked"] = last_checked
    if created_by is not None:
        update_data["created_by"] = created_by.lower()
    if user_id is not None:
        update_data["user_id"] = user_id

    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    current = clusters[cluster_index]
    updated = current.model_copy(update=update_data)
    validated = core_clusters.RedisCluster.model_validate(updated.model_dump())
    clusters[cluster_index] = validated

    if not await core_clusters.save_clusters(clusters):
        raise RuntimeError("Failed to save updated cluster")

    return {"id": validated.id, "status": "updated"}


async def delete_cluster_helper(cluster_id: str, *, confirm: bool = False) -> Dict[str, Any]:
    """Delete a cluster after explicit confirmation."""
    if not confirm:
        return {
            "error": "Confirmation required",
            "id": cluster_id,
            "status": "cancelled",
        }

    clusters = await core_clusters.get_clusters()
    remaining = [cluster for cluster in clusters if cluster.id != cluster_id]
    if len(remaining) == len(clusters):
        raise RuntimeError("Cluster not found")

    if not await core_clusters.save_clusters(remaining):
        raise RuntimeError("Failed to save after deletion")

    try:
        await core_clusters.delete_cluster_index_doc(cluster_id)
    except Exception:
        pass

    return {"id": cluster_id, "status": "deleted"}
