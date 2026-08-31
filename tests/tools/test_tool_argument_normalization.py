"""Tests for defensive normalization of model-generated tool arguments."""

from types import SimpleNamespace

from redis_sre_agent.tools.manager import ToolManager


def test_undeclared_tool_arguments_are_dropped() -> None:
    manager = ToolManager()
    manager._tool_by_name["list_targets"] = SimpleNamespace(
        definition=SimpleNamespace(
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            }
        )
    )

    normalized = manager._normalize_tool_args(
        "list_targets",
        {"limit": 5, "query": "production checkout cache"},
    )

    assert normalized == {"limit": 5}


def test_dynamic_tool_arguments_are_preserved_when_explicitly_allowed() -> None:
    manager = ToolManager()
    manager._tool_by_name["dynamic"] = SimpleNamespace(
        definition=SimpleNamespace(
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
        )
    )

    normalized = manager._normalize_tool_args("dynamic", {"custom": "value"})

    assert normalized == {"custom": "value"}
