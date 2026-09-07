"""Offline behavior contracts for the production routed-turn entrypoint."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.errors import GraphInterrupt

import redis_sre_agent.core.agent_execution as execution
import redis_sre_agent.core.turn_runner as tasks
import redis_sre_agent.core.turn_targeting as targeting
from redis_sre_agent.agent.models import AgentResponse
from redis_sre_agent.agent.router import AgentType
from redis_sre_agent.core.config import settings
from redis_sre_agent.core.tasks import TaskStatus


@pytest.fixture
def turn_runtime(monkeypatch):
    thread = SimpleNamespace(
        context={},
        messages=[],
        metadata=SimpleNamespace(user_id="reader", session_id="session-1", subject="Question"),
    )
    manager = SimpleNamespace(
        update_task_status=AsyncMock(),
        add_task_update=AsyncMock(),
        get_task_state=AsyncMock(return_value=SimpleNamespace(status=TaskStatus.IN_PROGRESS)),
        complete_task_if_open=AsyncMock(return_value=True),
        set_task_error=AsyncMock(),
        _publish_stream_update=AsyncMock(),
    )
    threads = SimpleNamespace(
        get_thread=AsyncMock(return_value=thread),
        append_messages=AsyncMock(),
        update_thread_context=AsyncMock(),
        _save_thread_state=AsyncMock(),
        set_message_trace=AsyncMock(),
    )
    agent = SimpleNamespace(
        process_query=AsyncMock(return_value=AgentResponse(response="Redis answer"))
    )
    monkeypatch.setattr(tasks, "ThreadManager", lambda **kwargs: threads)
    monkeypatch.setattr(tasks, "TaskManager", lambda **kwargs: manager)
    monkeypatch.setattr(execution, "get_chat_agent", lambda **kwargs: agent)
    monkeypatch.setattr(targeting, "_extract_instance_details_from_message", lambda message: None)
    monkeypatch.setattr(settings, "infrastructure_authorization_enabled", False)
    monkeypatch.setattr(settings, "semantic_cache_enabled", False)

    async def run(context=None):
        return await tasks.run_agent_turn(
            thread_id="thread-1",
            message="Explain Redis",
            task_id="task-1",
            context=context if context is not None else {"requested_agent_type": "chat"},
            redis_client=object(),
        )

    return SimpleNamespace(
        run=run,
        agent=agent,
        manager=manager,
        threads=threads,
        thread=thread,
    )


async def test_turn_persists_response_and_completion_event(turn_runtime):
    runtime = turn_runtime
    result = await runtime.run()
    assert result["response"] == "Redis answer"
    assert result["metadata"]["agent_type"] == "redis_chat"
    assert result["thread_id"] == "thread-1"
    assert result["task_id"] == "task-1"
    runtime.manager.complete_task_if_open.assert_awaited_once_with("task-1", result)
    assert [message.role for message in runtime.thread.messages] == ["user", "assistant"]
    assert runtime.thread.messages[-1].content == "Redis answer"
    event = runtime.manager._publish_stream_update.await_args.args
    assert event[0:2] == ("thread-1", "turn_complete")


async def test_cancelled_turn_does_not_persist_an_answer(turn_runtime):
    runtime = turn_runtime
    runtime.manager.get_task_state.return_value = SimpleNamespace(status=TaskStatus.CANCELLED)
    result = await runtime.run()
    assert result["status"] == "cancelled"
    runtime.manager.complete_task_if_open.assert_not_awaited()
    runtime.threads._save_thread_state.assert_not_awaited()


async def test_cancellation_during_completion_does_not_publish_success(turn_runtime):
    runtime = turn_runtime
    runtime.manager.complete_task_if_open.return_value = False
    result = await runtime.run()
    assert result["status"] == "cancelled"
    runtime.manager._publish_stream_update.assert_not_awaited()


async def test_raised_agent_error_marks_task_failed_and_propagates(turn_runtime):
    runtime = turn_runtime
    runtime.agent.process_query.side_effect = RuntimeError("model unavailable")
    with pytest.raises(RuntimeError, match="model unavailable"):
        await runtime.run()
    runtime.manager.set_task_error.assert_awaited_once_with(
        "task-1",
        "Agent turn failed: model unavailable",
    )
    runtime.manager.complete_task_if_open.assert_not_awaited()


async def test_graph_interrupt_uses_approval_transition(turn_runtime, monkeypatch):
    runtime = turn_runtime
    interruption = GraphInterrupt(())
    runtime.agent.process_query.side_effect = interruption
    transition = AsyncMock(return_value={"status": "awaiting_approval"})
    monkeypatch.setattr(tasks, "_transition_task_to_awaiting_approval_from_interrupt", transition)
    result = await runtime.run()
    assert result["status"] == "awaiting_approval"
    assert transition.await_args.kwargs["error"] is interruption
    runtime.manager.complete_task_if_open.assert_not_awaited()
    runtime.manager.set_task_error.assert_not_awaited()


async def test_knowledge_alias_keeps_its_metadata_and_iteration_budget(turn_runtime):
    runtime = turn_runtime
    result = await runtime.run({"requested_agent_type": "knowledge"})
    assert result["metadata"]["agent_type"] == "knowledge_only"
    assert runtime.agent.process_query.await_args.kwargs["max_iterations"] > 0


async def test_auto_mode_uses_router(turn_runtime, monkeypatch):
    router = AsyncMock(return_value=AgentType.REDIS_CHAT)
    monkeypatch.setattr(tasks, "route_to_appropriate_agent", router)
    await turn_runtime.run({})
    router.assert_awaited_once()
    assert router.await_args.kwargs["query"] == "Explain Redis"


async def test_explicit_instance_clears_previous_cluster_scope(turn_runtime, monkeypatch):
    runtime = turn_runtime
    runtime.thread.context = {"cluster_id": "old-cluster", "attached_target_handles": ["old"]}

    async def materialize(**kwargs):
        assert kwargs["legacy_instance_id"] == "new-instance"
        assert kwargs["legacy_cluster_id"] is None
        return kwargs["turn_scope"]

    monkeypatch.setattr(targeting, "_ensure_handle_backed_turn_scope", materialize)
    await runtime.run({"requested_agent_type": "chat", "instance_id": "new-instance"})
    updates = runtime.threads.update_thread_context.await_args.args[1]
    assert updates["instance_id"] == "new-instance"
    assert updates["cluster_id"] == ""
    assert updates["attached_target_handles"] == []
    assert runtime.agent.process_query.await_args.kwargs["context"]["instance_id"] == "new-instance"


async def test_denied_explicit_target_stops_before_agent(turn_runtime, monkeypatch):
    runtime = turn_runtime
    monkeypatch.setattr(settings, "infrastructure_authorization_enabled", True)
    monkeypatch.setattr(
        "redis_sre_agent.core.instances.get_instance_by_id", AsyncMock(return_value=None)
    )
    denied = AsyncMock(return_value={"response": "access denied"})
    monkeypatch.setattr(tasks, "_complete_turn_authorization_denied", denied)
    result = await runtime.run({"requested_agent_type": "chat", "instance_id": "denied"})
    assert result["response"] == "access denied"
    runtime.agent.process_query.assert_not_awaited()


async def test_conflicting_explicit_targets_fail_before_persisting_scope(turn_runtime):
    with pytest.raises(ValueError, match="only one of instance_id or cluster_id"):
        await turn_runtime.run({"instance_id": "instance", "cluster_id": "cluster"})
    turn_runtime.threads.update_thread_context.assert_not_awaited()
    turn_runtime.agent.process_query.assert_not_awaited()


@pytest.mark.parametrize("fails", [False, True])
async def test_turn_closes_its_span_and_restores_parent_context(turn_runtime, monkeypatch, fails):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("offline-turn-test")
    monkeypatch.setattr(tasks.trace, "get_tracer", lambda *args, **kwargs: tracer)
    observed_spans = []

    async def process_query(**kwargs):
        observed_spans.append(trace.get_current_span())
        if fails:
            raise RuntimeError("model unavailable")
        return AgentResponse(response="Redis answer")

    turn_runtime.agent.process_query.side_effect = process_query
    with tracer.start_as_current_span("caller") as parent:
        if fails:
            with pytest.raises(RuntimeError, match="model unavailable"):
                await turn_runtime.run()
        else:
            await turn_runtime.run()
        assert trace.get_current_span() is parent
        assert observed_spans[0].parent.span_id == parent.get_span_context().span_id
        assert observed_spans[0].end_time is not None
