"""Exercise approval recovery with real validation and offline agent/storage boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import redis_sre_agent.core.turn_approval as recovery
from redis_sre_agent.agent.models import AgentResponse
from redis_sre_agent.core.approvals import ApprovalRecord, ApprovalStatus, GraphResumeState
from redis_sre_agent.core.config import settings
from redis_sre_agent.core.tasks import TaskStatus
from redis_sre_agent.core.threads import Message


@pytest.fixture(params=["chat", "redis_triage"])
def resume_runtime(request, monkeypatch):
    graph_type = request.param
    record = ApprovalRecord(
        approval_id="approval-1",
        task_id="task-1",
        thread_id="thread-1",
        graph_thread_id="graph-1",
        interrupt_id="interrupt-1",
        graph_type=graph_type,
        graph_version="1",
        tool_name="redis_write",
        action_hash="approved-action",
    )
    resume = GraphResumeState(
        task_id="task-1",
        thread_id="thread-1",
        graph_thread_id="graph-1",
        graph_type=graph_type,
        graph_version="1",
        checkpoint_ns="",
        checkpoint_id="checkpoint-1",
        waiting_reason="approval_required",
        pending_approval_id=record.approval_id,
        pending_interrupt_id=record.interrupt_id,
    )
    state = SimpleNamespace(
        thread_id="thread-1",
        status=TaskStatus.AWAITING_APPROVAL,
        pending_approval=None,
        resume_supported=True,
        result=None,
    )
    thread = SimpleNamespace(
        context={},
        messages=[Message(role="user", content="Run approved action")],
        metadata=SimpleNamespace(session_id="session-1", user_id="reader", subject="Action"),
    )
    manager = SimpleNamespace(
        get_task_state=AsyncMock(return_value=state),
        set_pending_approval=AsyncMock(),
        set_resume_supported=AsyncMock(),
        update_task_status=AsyncMock(),
        add_task_update=AsyncMock(),
        complete_task_if_open=AsyncMock(return_value=True),
        set_task_result=AsyncMock(),
        set_task_error=AsyncMock(),
        _publish_stream_update=AsyncMock(),
    )
    threads = SimpleNamespace(
        get_thread=AsyncMock(return_value=thread),
        _save_thread_state=AsyncMock(),
        append_messages=AsyncMock(),
        set_message_trace=AsyncMock(),
    )
    approvals = SimpleNamespace(
        get_resume_state=AsyncMock(return_value=resume),
        get_approval=AsyncMock(return_value=record),
        record_decision=AsyncMock(return_value=record),
        save_resume_state=AsyncMock(),
        delete_resume_state=AsyncMock(),
        expire_approval=AsyncMock(),
    )
    response = AgentResponse(response="Action completed", tool_envelopes=[{"tool": "redis_write"}])

    async def resume_query(**kwargs):
        state.status = TaskStatus.IN_PROGRESS
        return response

    agent = SimpleNamespace(resume_query=AsyncMock(side_effect=resume_query))
    monkeypatch.setattr(recovery, "TaskManager", lambda **kwargs: manager)
    monkeypatch.setattr(recovery, "ThreadManager", lambda **kwargs: threads)
    monkeypatch.setattr(recovery, "ApprovalManager", lambda **kwargs: approvals)
    monkeypatch.setattr(recovery, "get_sre_agent", lambda **kwargs: agent)
    monkeypatch.setattr("redis_sre_agent.agent.chat_agent.ChatAgent", lambda **kwargs: agent)
    monkeypatch.setattr(recovery, "_resolve_instance_for_resume", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "infrastructure_authorization_enabled", False)

    async def run():
        return await recovery._resume_task_after_approval_impl(
            task_id="task-1",
            approval_id="approval-1",
            decision="approved",
            decision_by="reader",
            redis_client=object(),
        )

    return SimpleNamespace(
        run=run,
        graph_type=graph_type,
        record=record,
        resume=resume,
        state=state,
        thread=thread,
        manager=manager,
        threads=threads,
        approvals=approvals,
        agent=agent,
    )


async def test_resume_preserves_response_evidence_and_checkpoint_identity(resume_runtime):
    runtime = resume_runtime
    result = await runtime.run()
    call = runtime.agent.resume_query.await_args.kwargs
    assert call["session_id"] == "session-1"
    assert call["resume_payload"]["approval_id"] == "approval-1"
    assert call["resume_payload"]["action_hash"] == "approved-action"
    saved = runtime.approvals.save_resume_state.await_args.args[0]
    assert saved.checkpoint_id == "checkpoint-1"
    assert saved.graph_thread_id == "graph-1"
    assert saved.resume_count == 1
    runtime.manager.complete_task_if_open.assert_awaited_once_with("task-1", result)
    runtime.approvals.delete_resume_state.assert_awaited_once_with("task-1")
    assert runtime.threads.set_message_trace.await_args.kwargs["tool_envelopes"] == [
        {"tool": "redis_write"}
    ]
    if runtime.graph_type == "chat":
        assert result["response"]["response"] == "Action completed"
        assert result["instance_id"] is None
        assert result["cluster_id"] is None
        assert (
            runtime.threads.append_messages.await_args.args[1][0]["content"] == "Action completed"
        )
    else:
        assert result["response"] == "Action completed"
        assert result["metadata"]["agent_type"] == "redis_triage"
        assert [m.content for m in runtime.thread.messages] == [
            "Run approved action",
            "Action completed",
        ]
        runtime.threads._save_thread_state.assert_awaited_once()


async def test_cancelled_resume_does_not_publish_success_or_evidence(resume_runtime):
    runtime = resume_runtime
    runtime.manager.complete_task_if_open.return_value = False
    result = await runtime.run()
    assert result["status"] == "cancelled"
    runtime.approvals.delete_resume_state.assert_awaited_once_with("task-1")
    runtime.manager._publish_stream_update.assert_not_awaited()
    runtime.threads.set_message_trace.assert_not_awaited()


async def test_resume_rechecks_access_before_executing_tool(resume_runtime, monkeypatch):
    runtime = resume_runtime
    runtime.thread.context["instance_id"] = "revoked-target"
    monkeypatch.setattr(settings, "infrastructure_authorization_enabled", True)
    result = await runtime.run()
    assert result["metadata"]["authorization_denied"] is True
    runtime.agent.resume_query.assert_not_awaited()


async def test_expired_approval_cannot_resume(resume_runtime):
    runtime = resume_runtime
    runtime.record.status = ApprovalStatus.EXPIRED
    with pytest.raises(ValueError, match="has expired"):
        await runtime.run()
    runtime.agent.resume_query.assert_not_awaited()
    runtime.approvals.record_decision.assert_not_awaited()


async def test_mismatched_checkpoint_cannot_resume(resume_runtime):
    runtime = resume_runtime
    runtime.resume.graph_thread_id = "another-graph"
    with pytest.raises(ValueError, match="graph thread does not match"):
        await runtime.run()
    runtime.agent.resume_query.assert_not_awaited()
    runtime.approvals.record_decision.assert_not_awaited()
