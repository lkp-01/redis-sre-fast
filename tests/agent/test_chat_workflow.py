"""Exercise the real chat graph with a scripted model and offline tools."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

import redis_sre_agent.agent.chat_agent as chat


async def test_chat_graph_executes_tool_and_preserves_evidence(monkeypatch):
    tool_call = {
        "id": "call-1",
        "name": "redis_info",
        "args": {"section": "memory"},
        "type": "tool_call",
    }
    invoke = AsyncMock(
        side_effect=[
            AIMessage(content="", tool_calls=[tool_call]),
            AIMessage(content="Memory usage is 42 bytes."),
        ]
    )
    monkeypatch.setattr(chat, "guarded_ainvoke", invoke)
    monkeypatch.setattr(chat, "bind_tools_for_provider", lambda llm, tools: llm)
    tools = SimpleNamespace(
        get_toolset_generation=lambda: 1,
        get_tools_for_llm=lambda **kwargs: [],
        execute_tool_calls=AsyncMock(return_value=[{"status": "success", "used_memory": 42}]),
    )
    agent = object.__new__(chat.ChatAgent)
    agent.llm = object()
    graph = agent._build_workflow(tools).compile()
    result = await graph.ainvoke(
        {
            "messages": [SystemMessage(content="Answer from tool evidence.")],
            "session_id": "test",
            "user_id": None,
            "current_tool_calls": [],
            "iteration_count": 0,
            "max_iterations": 3,
            "startup_system_prompt": "Answer from tool evidence.",
            "startup_prompt_initialized": True,
            "signals_envelopes": [],
        }
    )
    tools.execute_tool_calls.assert_awaited_once_with(
        [{"name": "redis_info", "args": {"section": "memory"}}]
    )
    assert result["messages"][-1].content == "Memory usage is 42 bytes."
    assert result["signals_envelopes"][0]["data"]["used_memory"] == 42
    tool_messages = [m for m in invoke.await_args_list[1].args[1] if isinstance(m, ToolMessage)]
    assert tool_messages[0].tool_call_id == "call-1"
