"""Redis Cluster grouping metadata API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from redis_sre_agent.core import clusters as core_clusters
from redis_sre_agent.core import instances as core_instances

router = APIRouter()


async def _response(cluster: core_clusters.RedisCluster) -> dict:
    payload = cluster.model_dump(mode="json")
    payload.pop("extension_secrets", None)
    instances = await core_instances.get_instances()
    payload["associated_instance_ids"] = [
        instance.id for instance in instances if instance.cluster_id == cluster.id
    ]
    payload["diagnosable"] = bool(payload["associated_instance_ids"])
    return payload


class RedisClusterResponse(BaseModel):
    id: str
    name: str
    cluster_type: str
    environment: str
    description: str
    notes: Optional[str] = None
    status: Optional[str] = "unknown"
    version: Optional[str] = None
    last_checked: Optional[str] = None
    created_by: str = "user"
    user_id: Optional[str] = None
    created_at: str
    updated_at: str
    extension_data: Optional[dict] = None
    associated_instance_ids: list[str] = Field(default_factory=list)
    diagnosable: bool = False


class ClusterListResponse(BaseModel):
    clusters: list[RedisClusterResponse]
    total: int
    limit: int
    offset: int


class _ClusterFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    cluster_type: Optional[str] = Field(None, pattern="^oss_cluster$")
    environment: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    version: Optional[str] = None
    last_checked: Optional[str] = None
    created_by: Optional[str] = None
    user_id: Optional[str] = None


class CreateClusterRequest(_ClusterFields):
    name: str
    cluster_type: Literal["oss_cluster"] = "oss_cluster"
    environment: str
    description: str
    created_by: str = "user"


class UpdateClusterRequest(_ClusterFields):
    pass


@router.get("/clusters", response_model=ClusterListResponse)
async def list_clusters(
    environment: Optional[str] = None,
    status: Optional[str] = None,
    cluster_type: Optional[str] = Query(None, pattern="^oss_cluster$"),
    user_id: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> ClusterListResponse:
    result = await core_clusters.query_clusters(
        environment=environment,
        status=status,
        cluster_type=cluster_type,
        user_id=user_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return ClusterListResponse(
        clusters=[RedisClusterResponse(**await _response(cluster)) for cluster in result.clusters],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.post("/clusters", response_model=RedisClusterResponse, status_code=201)
async def create_cluster(request: CreateClusterRequest) -> RedisClusterResponse:
    clusters = await core_clusters.get_clusters()
    if any(cluster.name == request.name for cluster in clusters):
        raise HTTPException(status_code=400, detail=f"Cluster with name '{request.name}' already exists")
    try:
        cluster = core_clusters.RedisCluster(
            id=f"cluster-{request.environment.lower()}-{ULID()}",
            name=request.name,
            cluster_type="oss_cluster",
            environment=request.environment,
            description=request.description,
            notes=request.notes,
            status=request.status,
            version=request.version,
            last_checked=request.last_checked,
            created_by=request.created_by,
            user_id=request.user_id,
        )
        if not await core_clusters.save_clusters([*clusters, cluster]):
            raise RuntimeError("failed to save cluster")
        return RedisClusterResponse(**await _response(cluster))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/clusters/{cluster_id}", response_model=RedisClusterResponse)
async def get_cluster(cluster_id: str) -> RedisClusterResponse:
    cluster = await core_clusters.get_cluster_by_id(cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return RedisClusterResponse(**await _response(cluster))


@router.put("/clusters/{cluster_id}", response_model=RedisClusterResponse)
async def update_cluster(cluster_id: str, request: UpdateClusterRequest) -> RedisClusterResponse:
    clusters = await core_clusters.get_clusters()
    index = next((i for i, cluster in enumerate(clusters) if cluster.id == cluster_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    current = clusters[index]
    fields = request.model_dump(exclude_unset=True)
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        updated = core_clusters.RedisCluster.model_validate(current.model_dump() | fields)
        if updated.cluster_type.value != "oss_cluster":
            raise ValueError("cluster_type must be oss_cluster")
        clusters[index] = updated
        if not await core_clusters.save_clusters(clusters):
            raise RuntimeError("failed to save cluster")
        return RedisClusterResponse(**await _response(updated))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/clusters/{cluster_id}")
async def delete_cluster(cluster_id: str) -> dict[str, str]:
    clusters = await core_clusters.get_clusters()
    remaining = [cluster for cluster in clusters if cluster.id != cluster_id]
    if len(remaining) == len(clusters):
        raise HTTPException(status_code=404, detail="Cluster not found")
    if not await core_clusters.save_clusters(remaining):
        raise HTTPException(status_code=500, detail="Failed to save cluster changes")
    await core_clusters.delete_cluster_index_doc(cluster_id)
    return {"id": cluster_id, "status": "deleted"}
