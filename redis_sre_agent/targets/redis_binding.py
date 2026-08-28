"""Default Redis target binding strategy and client factories."""

from __future__ import annotations

from typing import Any

from redis_sre_agent.core.config import settings
from redis_sre_agent.core.instances import RedisInstance, get_instance_by_id

from .contracts import BindingRequest, BindingResult, ProviderLoadRequest, TargetHandleRecord


def _eval_target_seed(handle_record: TargetHandleRecord) -> dict[str, Any] | None:
    seed = (handle_record.private_binding_ref or {}).get("eval_target_seed")
    return seed if isinstance(seed, dict) else None


def _build_seeded_instance(handle_record: TargetHandleRecord) -> RedisInstance | None:
    seed = _eval_target_seed(handle_record)
    if not seed or seed.get("seed_kind") != "instance":
        return None
    payload = dict(seed)
    payload["id"] = handle_record.target_handle
    payload.setdefault("created_by", "agent")
    payload.setdefault("user_id", "eval")
    return RedisInstance.model_validate(payload)


class RedisDataClientFactory:
    """Return a RedisInstance for instance-scoped provider loads."""

    client_family = "redis.data"

    async def build(self, handle_record: TargetHandleRecord) -> Any:
        if handle_record.public_summary.target_kind != "instance":
            return None
        instance = await get_instance_by_id(handle_record.binding_subject)
        if instance is None:
            return _build_seeded_instance(handle_record)
        return instance.model_copy(update={"id": handle_record.target_handle})


class RedisTargetBindingStrategy:
    """Bind only RedisInstance records to standard Redis diagnostic providers."""

    strategy_name = "redis_default"

    async def bind(self, request: BindingRequest) -> BindingResult:
        from .registry import get_target_integration_registry

        registry = get_target_integration_registry()
        handle_record = request.handle_record
        public_summary = handle_record.public_summary

        if public_summary.target_kind != "instance":
            return BindingResult(public_summary=public_summary)

        data_instance = await registry.get_client_factory("redis.data").build(handle_record)
        if data_instance is None:
            return BindingResult(public_summary=public_summary)

        provider_loads = [
            ProviderLoadRequest(
                provider_path=provider_path,
                provider_key=f"target:{public_summary.target_handle}:{provider_path}",
                target_handle=public_summary.target_handle,
                provider_context={"redis_instance_override": data_instance},
            )
            for provider_path in settings.tool_providers
        ]

        return BindingResult(
            public_summary=public_summary,
            provider_loads=provider_loads,
            client_refs={"redis.data": data_instance},
        )
