"""Keep the historical MCP task response shapes and options stable."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import redis_sre_agent.core.docket_tasks as tasks
from redis_sre_agent.agent.models import AgentResponse
from redis_sre_agent.core.tasks import TaskStatus
from redis_sre_agent.core.threads import Message
from redis_sre_agent.tools.models import ToolCapability


@pytest.fixture
def legacy_runtime(monkeypatch):
    response = AgentResponse(
        response="legacy answer",
        tool_envelopes=[
            {
                "tool_key": "redis_info",
                "status": "success",
                "data": {"role": "master"},
            }
        ],
    )
    manager = SimpleNamespace(
        update_task_status=AsyncMock(),
        add_task_update=AsyncMock(),
        get_task_state=AsyncMock(return_value=SimpleNamespace(status=TaskStatus.IN_PROGRESS)),
        set_task_result=AsyncMock(),
        set_task_error=AsyncMock(),
    )
    thread = SimpleNamespace(
        messages=[
            Message(role="user", content="previous"),
            Message(role="assistant", content="previous answer"),
            Message(role="user", content="question"),
        ]
    )
    threads = SimpleNamespace(
        get_thread=AsyncMock(return_value=thread),
        append_messages=AsyncMock(),
        set_message_trace=AsyncMock(),
    )
    agent = SimpleNamespace(process_query=AsyncMock(return_value=response))
    factory = Mock(return_value=agent)
    monkeypatch.setattr(tasks, "get_redis_client", lambda: object())
    monkeypatch.setattr(tasks, "TaskManager", lambda **kwargs: manager)
    monkeypatch.setattr(tasks, "ThreadManager", lambda **kwargs: threads)
    monkeypatch.setattr(tasks, "get_chat_agent", factory)
    monkeypatch.setattr("redis_sre_agent.agent.chat_agent.ChatAgent", factory)
    return SimpleNamespace(
        agent=agent, manager=manager, threads=threads, factory=factory, response=response
    )


async def test_legacy_database_chat_keeps_filter_and_nested_response(legacy_runtime):
    runtime = legacy_runtime
    result = await tasks._process_chat_turn_impl(
        query="question",
        task_id="task",
        thread_id="thread",
        exclude_mcp_categories=["logs"],
    )
    assert result == {
        "response": runtime.response.model_dump(),
        "instance_id": None,
        "cluster_id": None,
    }
    assert runtime.factory.call_args.kwargs["exclude_mcp_categories"] == [ToolCapability.LOGS]
    runtime.manager.set_task_result.assert_awaited_once_with("task", result)
    runtime.threads.set_message_trace.assert_awaited_once()
    assert runtime.threads.append_messages.await_args.args[1][0]["content"] == "legacy answer"


async def test_legacy_knowledge_keeps_history_without_duplicate_current_question(legacy_runtime):
    runtime = legacy_runtime
    result = await tasks.process_knowledge_query("question", "task", "thread")
    assert result == {"response": runtime.response.model_dump()}
    history = runtime.agent.process_query.await_args.kwargs["conversation_history"]
    assert [message.content for message in history] == ["previous", "previous answer"]
    runtime.manager.set_task_result.assert_awaited_once_with("task", result)


def test_registered_task_names_and_import_paths_are_unchanged():
    expected = {
        "search_knowledge_base",
        "ingest_sre_document",
        "embed_qa_record",
        "process_chat_turn",
        "process_knowledge_query",
        "process_pipeline_operation",
        "scheduler_task",
        "resume_task_after_approval",
        "process_agent_turn",
    }
    assert {task.__name__ for task in tasks.SRE_TASK_COLLECTION} == expected
    assert all(
        task.__module__ == "redis_sre_agent.core.docket_tasks" for task in tasks.SRE_TASK_COLLECTION
    )
