"""Tests for strict LLM-facing tool adapter schemas."""

import pytest
from pydantic import ValidationError

from redis_sre_agent.agent.helpers import build_adapters_for_tooldefs
from redis_sre_agent.tools.target_discovery.provider import TargetDiscoveryToolProvider


@pytest.mark.asyncio
async def test_tool_adapters_forbid_undeclared_arguments() -> None:
    definitions = TargetDiscoveryToolProvider().create_tool_schemas()

    adapters = await build_adapters_for_tooldefs(None, definitions)

    list_targets = next(adapter for adapter in adapters if adapter.name.endswith("list_known_redis_targets"))
    assert list_targets.args_schema.model_json_schema()["additionalProperties"] is False
    with pytest.raises(ValidationError):
        list_targets.args_schema(query="production checkout cache")
