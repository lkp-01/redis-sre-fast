"""Regression tests for knowledge-agent provider failures."""

from redis_sre_agent.agent.knowledge_agent import KNOWLEDGE_LLM_REQUEST_FAILED_MESSAGE


def test_provider_failure_message_does_not_blame_the_query() -> None:
    message = KNOWLEDGE_LLM_REQUEST_FAILED_MESSAGE.lower()

    assert "rephras" not in message
    assert "provider" in message
    assert "tool-calling compatibility" in message
