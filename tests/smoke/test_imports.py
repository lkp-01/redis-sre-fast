"""Smoke tests for the public runtime entry points."""


def test_runtime_entry_points_import() -> None:
    """Core entry points remain importable from a clean process."""
    from redis_sre_agent.api.app import app
    from redis_sre_agent.mcp_server.server import mcp
    from redis_sre_agent.targets import get_target_integration_registry
    from redis_sre_agent.tools.manager import ToolManager

    assert app is not None
    assert mcp is not None
    assert ToolManager is not None
    assert get_target_integration_registry is not None
