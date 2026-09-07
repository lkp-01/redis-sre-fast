"""Invoke agents with prepared conversation and target context."""

from __future__ import annotations

import logging
from typing import Any, List

from langchain_core.messages import AIMessage, HumanMessage

from redis_sre_agent.agent import get_sre_agent
from redis_sre_agent.agent.chat_agent import get_chat_agent
from redis_sre_agent.agent.langgraph_agent import SRELangGraphAgent
from redis_sre_agent.agent.router import AgentType
from redis_sre_agent.core.clusters import get_cluster_by_id
from redis_sre_agent.core.config import settings
from redis_sre_agent.core.targets import (
    get_attached_target_handles_from_context,
)
from redis_sre_agent.core.threads import Message
from redis_sre_agent.core.turn_targeting import PreparedTarget, _resolve_instance_for_thread

logger = logging.getLogger(__name__)


def _thread_messages_to_conversation_history(thread_messages: List[Message]) -> List[Any]:
    """Convert persisted thread messages into LangChain conversation history."""
    history: List[Any] = []
    for msg in thread_messages:
        if msg.role == "user":
            history.append(HumanMessage(content=msg.content))
        elif msg.role == "assistant":
            history.append(AIMessage(content=msg.content))
    return history


async def run_agent_with_progress(
    agent: SRELangGraphAgent,
    conversation_state,
    progress_emitter,
    thread_state=None,
    agent_context=None,
) -> dict[str, Any]:
    """Invoke the supplied agent with the current question and prior conversation."""
    from langchain_core.messages import SystemMessage

    try:
        messages = conversation_state.get("messages", [])
        if not messages:
            raise ValueError("No messages in conversation")
        message_types = {"user": HumanMessage, "assistant": AIMessage, "system": SystemMessage}
        history = [
            message_types[m["role"]](content=m["content"])
            for m in messages
            if m["role"] in message_types
        ]
        query = next((m["content"] for m in reversed(messages) if m["role"] == "user"), None)
        if not query:
            raise ValueError("No user message found in conversation")
        if agent_context is None:
            agent_context = thread_state.context if thread_state else None
        thread_id = conversation_state.get("thread_id", "default")
        response = await agent.process_query(
            query=query,
            session_id=thread_id,
            user_id=thread_state.metadata.user_id if thread_state else "system",
            max_iterations=settings.max_iterations,
            context=agent_context,
            progress_emitter=progress_emitter,
            conversation_history=history[:-1] if history else None,
        )
        await progress_emitter.emit("Agent workflow completed", "agent_complete")
        return {
            "response": response.response,
            "search_results": response.search_results,
            "tool_envelopes": response.tool_envelopes,
            "metadata": {
                "iterations": 1,
                "tool_calls": len(response.tool_envelopes),
                "session_id": thread_id,
            },
        }
    except Exception as exc:
        await progress_emitter.emit(f"Agent error: {exc}", "error")
        raise


async def select_turn_agent(*, agent_type: AgentType, target: PreparedTarget, thread_id: str):
    """Construct one agent for this execution, using resolved scope instead of stale bindings."""
    explicit_client_scope = target.explicit_scope
    current_scope = target.scope
    routing_context = target.context
    active_instance_id = target.instance_id
    active_cluster_id = target.cluster_id
    staged_thread_instance = target.staged_instance
    if agent_type == AgentType.REDIS_TRIAGE:
        agent = get_sre_agent()
    else:
        if (
            explicit_client_scope
            or current_scope.scope_kind == "target_bindings"
            or len(get_attached_target_handles_from_context(routing_context)) > 1
        ):
            # Explicit client-selected scope should flow through routing context so
            # stale direct bindings never survive handle-backed scope rebuilds.
            target_instance = None
            target_cluster = None
        else:
            # Get the target instance for the chat agent
            target_instance = None
            if active_instance_id:
                if (
                    staged_thread_instance is not None
                    and staged_thread_instance.id == active_instance_id
                ):
                    target_instance = staged_thread_instance
                else:
                    target_instance = await _resolve_instance_for_thread(
                        active_instance_id, thread_id
                    )
            target_cluster = (
                await get_cluster_by_id(active_cluster_id) if active_cluster_id else None
            )
        agent = get_chat_agent(
            redis_instance=target_instance,
            redis_cluster=target_cluster,
        )

    return agent


async def execute_selected_agent(
    *,
    agent,
    agent_type: AgentType,
    message,
    thread,
    thread_id,
    task_id,
    target: PreparedTarget,
    conversation_state,
    progress_emitter,
    task_manager,
) -> dict[str, Any]:
    """Keep chat/knowledge-cache and deep-triage policies at one invocation boundary."""
    routing_context = target.context
    if agent_type != AgentType.REDIS_TRIAGE:
        # Use lightweight chat agent with process_query interface
        is_knowledge_only = agent_type == AgentType.KNOWLEDGE_ONLY
        await task_manager.add_task_update(
            task_id,
            "Processing query with knowledge-only agent"
            if is_knowledge_only
            else "Processing query with chat agent",
            "agent_processing",
        )

        # Convert conversation history to LangChain messages
        lc_history = []
        for msg in conversation_state["messages"][:-1]:  # Exclude the latest message
            if msg["role"] == "user":
                lc_history.append(HumanMessage(content=msg["content"]))
            elif msg["role"] == "assistant":
                lc_history.append(AIMessage(content=msg["content"]))

        if is_knowledge_only:
            max_iterations = settings.knowledge_max_iterations
            if not isinstance(max_iterations, int) or max_iterations <= 0:
                max_iterations = min(int(settings.max_iterations or 10), 8)
        else:
            max_iterations = min(int(settings.max_iterations or 15), 10)

        # Semantic answer cache: only the knowledge-only turn is instance-
        # independent and therefore safe to serve globally (chat/triage turns
        # depend on a specific instance's live state). Default OFF; fail-open.
        cache_history = lc_history if lc_history else None
        cached_response = None
        cache_key = None
        if is_knowledge_only and settings.semantic_cache_enabled:
            from redis_sre_agent.core.semantic_cache.service import lookup_cached_answer

            cached_response, cache_key = await lookup_cached_answer(message, cache_history)

        if cached_response is not None:
            await task_manager.add_task_update(
                task_id, "Served answer from semantic cache", "agent_processing"
            )
            chat_agent_response = cached_response
        else:
            chat_agent_response = await agent.process_query(
                query=message,
                user_id=thread.metadata.user_id,
                session_id=thread.metadata.session_id or thread_id,
                max_iterations=max_iterations,
                context=routing_context,
                progress_emitter=progress_emitter,
                conversation_history=cache_history,
            )
            if is_knowledge_only and settings.semantic_cache_enabled:
                from redis_sre_agent.core.semantic_cache.service import schedule_store

                schedule_store(
                    message,
                    chat_agent_response.response,
                    chat_agent_response.search_results,
                    cache_history,
                    rewritten_query=cache_key,
                )

        # chat_agent_response is an AgentResponse with .response, .search_results, .tool_envelopes
        agent_response = {
            "response": chat_agent_response.response,
            "search_results": chat_agent_response.search_results,
            "tool_envelopes": chat_agent_response.tool_envelopes,
            "metadata": {"agent_type": "knowledge_only" if is_knowledge_only else "redis_chat"},
        }
    else:
        # Use full Redis triage agent with full conversation state
        agent_response = await run_agent_with_progress(
            agent,
            conversation_state,
            progress_emitter,
            thread,
            agent_context=routing_context,
        )

    return agent_response
