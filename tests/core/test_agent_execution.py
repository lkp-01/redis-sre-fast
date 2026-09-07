"""Agent invocation and fan-out isolation, without network clients."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from redis_sre_agent.agent.models import AgentResponse
from redis_sre_agent.core.agent_execution import run_agent_with_progress
from redis_sre_agent.core.turn_fanout import _run_single_target_triage_child


async def test_progress_wrapper_uses_supplied_agent_and_preserves_history():
    agent = SimpleNamespace(process_query=AsyncMock(return_value=AgentResponse(response="answer")))
    emitter = SimpleNamespace(emit=AsyncMock())
    context = {"instance_id": "current"}
    result = await run_agent_with_progress(
        agent,
        {
            "thread_id": "session",
            "messages": [
                {"role": "user", "content": "previous question"},
                {"role": "assistant", "content": "previous answer"},
                {"role": "user", "content": "current question"},
            ],
        },
        emitter,
        SimpleNamespace(metadata=SimpleNamespace(user_id="reader")),
        agent_context=context,
    )
    call = agent.process_query.await_args.kwargs
    assert call["query"] == "current question"
    assert [message.content for message in call["conversation_history"]] == [
        "previous question",
        "previous answer",
    ]
    assert call["context"] is context
    assert call["progress_emitter"] is emitter
    assert result["response"] == "answer"


async def test_concurrent_triage_children_have_independent_agents_and_contexts(monkeypatch):
    created = []

    def create_agent():
        async def query(**kwargs):
            await asyncio.sleep(0)
            return AgentResponse(response=kwargs["context"]["task_id"])

        agent = SimpleNamespace(process_query=AsyncMock(side_effect=query))
        created.append(agent)
        return agent

    monkeypatch.setattr("redis_sre_agent.core.turn_fanout.get_sre_agent", create_agent)
    tasks = SimpleNamespace(
        update_task_status=AsyncMock(),
        add_task_update=AsyncMock(),
        complete_task_if_open=AsyncMock(return_value=True),
        _publish_stream_update=AsyncMock(),
    )
    threads = SimpleNamespace(append_messages=AsyncMock(), set_message_trace=AsyncMock())
    from redis_sre_agent.core.targets import TargetBinding

    bindings = [
        TargetBinding(
            target_handle=f"target-{i}",
            target_kind="instance",
            resource_id=f"redis-{i}",
            display_name=f"Redis {i}",
            capabilities=["redis"],
            thread_id="thread",
        )
        for i in range(2)
    ]
    results = await asyncio.gather(
        *[
            _run_single_target_triage_child(
                binding=binding,
                child_task_id=f"child-{i}",
                parent_task_id="parent",
                thread_id="thread",
                thread=SimpleNamespace(metadata=SimpleNamespace(user_id="reader")),
                task_manager=tasks,
                thread_manager=threads,
                base_conversation_state={"messages": [{"role": "user", "content": "triage both"}]},
                base_context={},
                original_query="triage both",
                generation=1,
            )
            for i, binding in enumerate(bindings)
        ]
    )
    assert len(created) == 2
    assert created[0] is not created[1]
    assert {result["response"] for result in results} == {"child-0", "child-1"}
    for i, agent in enumerate(created):
        call = agent.process_query.await_args.kwargs
        assert call["context"]["task_id"] == f"child-{i}"
        assert call["context"]["attached_target_handles"] == [f"target-{i}"]
        assert call["session_id"] == f"thread:child-{i}"
