"""Public-management surface regression tests for the OSS-only boundary."""

from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from redis_sre_agent.api.clusters import CreateClusterRequest
from redis_sre_agent.api.instances import CreateInstanceRequest
from redis_sre_agent.cli.main import main
from redis_sre_agent.mcp_server.server import mcp


def test_api_models_reject_removed_management_fields() -> None:
    instance_payload = {
        "name": "cache-a",
        "connection_url": "redis://localhost:6379/0",
        "environment": "test",
        "usage": "cache",
        "description": "test endpoint",
    }
    with pytest.raises(ValidationError):
        CreateInstanceRequest.model_validate(instance_payload | {"admin_url": "https://invalid"})
    with pytest.raises(ValidationError):
        CreateClusterRequest.model_validate(
            {"name": "cluster-a", "environment": "test", "description": "test", "cluster_type": "redis_enterprise"}
        )


def test_cli_subcommands_do_not_advertise_removed_flags() -> None:
    runner = CliRunner()
    instance_help = runner.invoke(main, ["instance", "create", "--help"])
    cluster_help = runner.invoke(main, ["cluster", "create", "--help"])

    assert instance_help.exit_code == 0, instance_help.output
    assert cluster_help.exit_code == 0, cluster_help.output
    combined_help = f"{instance_help.output}\n{cluster_help.output}".lower()
    assert "admin-url" not in combined_help
    assert "redis-cloud" not in combined_help


def test_mcp_tools_do_not_expose_removed_provider_operations() -> None:
    tools = asyncio.run(mcp.list_tools())
    serialized = "\n".join(
        f"{tool.name}\n{tool.description}\n{tool.inputSchema}" for tool in tools
    ).lower()

    assert "support-package" not in serialized
    assert "redis-enterprise" not in serialized
    assert "redis_cloud" not in serialized
