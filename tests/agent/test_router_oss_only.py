import pytest

from redis_sre_agent.agent.router import AgentType, route_to_appropriate_agent


@pytest.mark.asyncio
async def test_removed_support_package_context_does_not_force_triage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args, **kwargs):
        raise RuntimeError("routing model unavailable")

    monkeypatch.setattr("redis_sre_agent.agent.router.create_nano_llm", fail_if_called)

    result = await route_to_appropriate_agent(
        "What is Redis replication?",
        context={"support_package_path": "/tmp/legacy-package"},
    )

    assert result is AgentType.REDIS_CHAT
