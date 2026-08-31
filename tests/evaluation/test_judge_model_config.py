from __future__ import annotations

from redis_sre_agent.evaluation import judge as judge_module


def test_judge_uses_configured_mini_model_mapping(monkeypatch) -> None:
    calls = []
    sentinel = object()

    def fake_create_mini_llm(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(judge_module, "create_mini_llm", fake_create_mini_llm)

    judge = judge_module.SREAgentJudge()

    assert judge.llm is sentinel
    assert calls == [((), {})]

