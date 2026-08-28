"""Target binding tests for the OSS-only diagnostic boundary."""

import pytest

from redis_sre_agent.core.config import settings
from redis_sre_agent.targets.contracts import (
    BindingRequest,
    PublicTargetBinding,
    TargetHandleRecord,
)
from redis_sre_agent.targets.redis_binding import RedisTargetBindingStrategy


def _handle(target_kind: str) -> TargetHandleRecord:
    return TargetHandleRecord(
        target_handle="target-1",
        discovery_backend="redis_catalog",
        binding_strategy="redis_default",
        binding_subject="resource-1",
        public_summary=PublicTargetBinding(
            target_handle="target-1",
            target_kind=target_kind,
            display_name="orders",
        ),
    )


@pytest.mark.asyncio
async def test_cluster_metadata_produces_no_provider_loads() -> None:
    result = await RedisTargetBindingStrategy().bind(BindingRequest(handle_record=_handle("cluster")))

    assert result.provider_loads == []
    assert result.client_refs == {}


def test_default_target_factories_only_include_redis_data() -> None:
    assert set(settings.target_integrations.client_factories) == {"redis.data"}
