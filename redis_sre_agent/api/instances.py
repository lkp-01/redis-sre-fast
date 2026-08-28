"""Redis protocol endpoint management API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from ulid import ULID

from redis_sre_agent.core import instances as core_instances
from redis_sre_agent.core.instance_mutation_helpers import _validate_instance_cluster_link
from redis_sre_agent.core.redis_topology import classify_redis_endpoint

router = APIRouter()


def _response(instance: core_instances.RedisInstance) -> dict:
    payload = instance.model_dump(mode="json")
    payload["connection_url"] = core_instances.mask_redis_url(instance.connection_url)
    payload.pop("extension_secrets", None)
    return payload


class RedisInstanceResponse(BaseModel):
    id: str
    name: str
    connection_url: str
    environment: str
    usage: str
    description: str
    instance_type: str
    cluster_id: Optional[str] = None
    status: Optional[str] = "unknown"
    repo_url: Optional[str] = None
    notes: Optional[str] = None
    monitoring_identifier: Optional[str] = None
    logging_identifier: Optional[str] = None
    version: Optional[str] = None
    memory: Optional[str] = None
    connections: Optional[int] = None
    last_checked: Optional[str] = None
    created_at: str
    updated_at: str
    created_by: str = "user"
    user_id: Optional[str] = None
    extension_data: Optional[dict] = None


class InstanceListResponse(BaseModel):
    instances: list[RedisInstanceResponse]
    total: int
    limit: int
    offset: int


class _InstanceFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    connection_url: Optional[SecretStr] = None
    environment: Optional[str] = None
    usage: Optional[str] = None
    description: Optional[str] = None
    repo_url: Optional[str] = None
    notes: Optional[str] = None
    monitoring_identifier: Optional[str] = None
    logging_identifier: Optional[str] = None
    instance_type: Optional[str] = Field(
        None, description="Optional asserted type; it must match protocol probing."
    )
    cluster_id: Optional[str] = None
    status: Optional[str] = None
    version: Optional[str] = None
    memory: Optional[str] = None
    connections: Optional[int] = None
    created_by: Optional[str] = None
    user_id: Optional[str] = None

    @field_validator("connection_url")
    @classmethod
    def validate_connection_url(cls, value: Optional[SecretStr]) -> Optional[SecretStr]:
        if value is None:
            return value
        parsed = urlparse(value.get_secret_value())
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("connection_url must be a redis:// or rediss:// URL with a hostname")
        return value


class CreateInstanceRequest(_InstanceFields):
    name: str
    connection_url: SecretStr
    environment: str
    usage: str
    description: str
    created_by: str = "user"


class UpdateInstanceRequest(_InstanceFields):
    pass


@router.get("/instances", response_model=InstanceListResponse)
async def list_instances(
    environment: Optional[str] = None,
    usage: Optional[str] = None,
    status: Optional[str] = None,
    instance_type: Optional[str] = Query(None, pattern="^(oss_single|oss_cluster)$"),
    user_id: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> InstanceListResponse:
    result = await core_instances.query_instances(
        environment=environment,
        usage=usage,
        status=status,
        instance_type=instance_type,
        user_id=user_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return InstanceListResponse(
        instances=[RedisInstanceResponse(**_response(instance)) for instance in result.instances],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.post("/instances", response_model=RedisInstanceResponse, status_code=201)
async def create_instance(request: CreateInstanceRequest) -> RedisInstanceResponse:
    instances = await core_instances.get_instances()
    if any(instance.name == request.name for instance in instances):
        raise HTTPException(status_code=400, detail=f"Instance with name '{request.name}' already exists")
    try:
        connection_url = request.connection_url.get_secret_value()
        detected_type = await classify_redis_endpoint(connection_url, request.instance_type)
        cluster_id = await _validate_instance_cluster_link(
            cluster_id=request.cluster_id, instance_type=detected_type.value
        )
        instance = core_instances.RedisInstance(
            id=f"redis-{request.environment.lower()}-{ULID()}",
            name=request.name,
            connection_url=request.connection_url,
            environment=request.environment.lower(),
            usage=request.usage.lower(),
            description=request.description,
            repo_url=request.repo_url,
            notes=request.notes,
            monitoring_identifier=request.monitoring_identifier,
            logging_identifier=request.logging_identifier,
            instance_type=detected_type,
            cluster_id=cluster_id,
            status=request.status,
            version=request.version,
            memory=request.memory,
            connections=request.connections,
            created_by=request.created_by,
            user_id=request.user_id,
        )
        if not await core_instances.save_instances([*instances, instance]):
            raise RuntimeError("failed to save instance")
        return RedisInstanceResponse(**_response(instance))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/instances/{instance_id}", response_model=RedisInstanceResponse)
async def get_instance(instance_id: str) -> RedisInstanceResponse:
    instance = await core_instances.get_instance_by_id(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    return RedisInstanceResponse(**_response(instance))


@router.put("/instances/{instance_id}", response_model=RedisInstanceResponse)
async def update_instance(instance_id: str, request: UpdateInstanceRequest) -> RedisInstanceResponse:
    instances = await core_instances.get_instances()
    index = next((i for i, instance in enumerate(instances) if instance.id == instance_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    current = instances[index]
    fields = request.model_dump(exclude_unset=True)
    try:
        connection_url = fields.pop("connection_url", None)
        explicit_type = fields.pop("instance_type", None)
        if connection_url is not None or explicit_type is not None:
            effective_url = (
                connection_url.get_secret_value()
                if connection_url is not None
                else current.connection_url.get_secret_value()
            )
            fields["instance_type"] = await classify_redis_endpoint(
                effective_url, explicit_type or current.instance_type
            )
        if connection_url is not None:
            fields["connection_url"] = connection_url
        if "cluster_id" in fields or "instance_type" in fields:
            fields["cluster_id"] = await _validate_instance_cluster_link(
                cluster_id=fields.get("cluster_id", current.cluster_id),
                instance_type=fields.get("instance_type", current.instance_type).value,
            )
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        updated = core_instances.RedisInstance.model_validate(current.model_dump() | fields)
        instances[index] = updated
        if not await core_instances.save_instances(instances):
            raise RuntimeError("failed to save instance")
        return RedisInstanceResponse(**_response(updated))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/instances/{instance_id}")
async def delete_instance(instance_id: str) -> dict[str, str]:
    instances = await core_instances.get_instances()
    remaining = [instance for instance in instances if instance.id != instance_id]
    if len(remaining) == len(instances):
        raise HTTPException(status_code=404, detail="Instance not found")
    if not await core_instances.save_instances(remaining):
        raise HTTPException(status_code=500, detail="Failed to save instance changes")
    await core_instances.delete_instance_index_doc(instance_id)
    return {"id": instance_id, "status": "deleted"}


class TestConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_url: SecretStr


@router.post("/instances/test-connection-url")
async def test_connection_url(request: TestConnectionRequest) -> dict[str, object]:
    from redis_sre_agent.core.redis_topology import probe_redis_topology

    result = await probe_redis_topology(request.connection_url.get_secret_value())
    return result.model_dump(mode="json")


@router.post("/instances/{instance_id}/test-connection")
async def test_instance_connection(instance_id: str) -> dict[str, object]:
    instance = await core_instances.get_instance_by_id(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    from redis_sre_agent.core.redis_topology import probe_redis_topology

    return (await probe_redis_topology(instance.connection_url.get_secret_value())).model_dump(mode="json")
