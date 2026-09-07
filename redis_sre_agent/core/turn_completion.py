"""Conversation persistence and task completion for agent turns."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ulid import ULID

from redis_sre_agent.agent.models import AgentResponse
from redis_sre_agent.core.citation_message import (
    build_citation_group_payloads,
)
from redis_sre_agent.core.qa import QAManager
from redis_sre_agent.core.tasks import TaskManager, TaskStatus
from redis_sre_agent.core.threads import Message, ThreadManager
from redis_sre_agent.targets.contracts import (
    DISCOVERY_STATUS_TOO_MANY_MATCHES,
    MULTI_TARGET_SELECTION_LIMIT,
)

logger = logging.getLogger(__name__)


def _extract_pending_approval_from_response(response: Any) -> Optional[Dict[str, Any]]:
    """Return pending approval metadata from an agent response, if present."""

    tool_envelopes = (
        response.get("tool_envelopes", [])
        if isinstance(response, dict)
        else getattr(response, "tool_envelopes", None) or []
    )
    for envelope in tool_envelopes:
        if not isinstance(envelope, dict):
            continue
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("status") != "approval_required":
            continue
        pending_approval = data.get("pending_approval")
        if isinstance(pending_approval, dict):
            return pending_approval
    return None


def _target_binding_metadata(binding: Any) -> Dict[str, Any]:
    """Return stable, public metadata for a target binding."""
    return {
        "target_handle": getattr(binding, "target_handle", ""),
        "target_kind": getattr(binding, "target_kind", ""),
        "display_name": getattr(binding, "display_name", ""),
        "capabilities": list(getattr(binding, "capabilities", None) or []),
    }


def _format_target_limit_response(
    *,
    resolution: Any = None,
    bindings: Optional[List[Any]] = None,
) -> str:
    binding_list = list(bindings or [])
    max_selectable = int(
        getattr(resolution, "max_selectable", None) or MULTI_TARGET_SELECTION_LIMIT
    )
    match_count = int(
        getattr(resolution, "match_count", None)
        or len(getattr(resolution, "matches", []) or [])
        or len(binding_list)
    )
    matches = list(getattr(resolution, "matches", None) or [])
    labels: List[str] = []
    for match in matches[:max_selectable]:
        label = str(getattr(match, "display_name", "") or "").strip()
        kind = str(getattr(match, "target_kind", "") or "").strip()
        labels.append(f"- {label} ({kind})" if kind else f"- {label}")
    for binding in binding_list[:max_selectable]:
        metadata = _target_binding_metadata(binding)
        label = metadata["display_name"] or metadata["target_handle"]
        kind = metadata["target_kind"]
        labels.append(f"- {label} ({kind})" if kind else f"- {label}")

    lines = [
        f"I found {match_count} Redis targets, which is more than I can deep triage in one request.",
        f"Please narrow the request to {max_selectable} or fewer targets, then I can run one deep triage session per target and return each report as it completes.",
    ]
    if labels:
        lines.extend(["", "Some matching targets:", *labels])
    return "\n".join(lines)


def _build_terminal_task_result(task_id: str, thread_id: str, status: TaskStatus) -> Dict[str, Any]:
    return {
        "status": status.value,
        "thread_id": thread_id,
        "task_id": task_id,
    }


async def _complete_task_if_open(
    *,
    task_manager: TaskManager,
    task_id: str,
    thread_id: str,
    result: Dict[str, Any],
) -> bool:
    completed = await task_manager.complete_task_if_open(task_id, result)
    if completed:
        return True

    try:
        task_state = await task_manager.get_task_state(task_id)
    except Exception:
        task_state = None
    status = task_state.status.value if task_state else "missing"
    logger.info(
        "Skipped successful completion for task %s because current status is %s",
        task_id,
        status,
    )
    return False


async def _complete_deep_triage_target_limit_response(
    *,
    task_manager: TaskManager,
    thread_manager: ThreadManager,
    thread_id: str,
    task_id: str,
    user_message: str,
    resolution: Any = None,
    bindings: Optional[List[Any]] = None,
    append_user_message: bool = True,
) -> Dict[str, Any]:
    """Complete a deep-triage turn that matched more targets than fan-out should run."""
    binding_list = list(bindings or [])
    response_text = _format_target_limit_response(
        resolution=resolution,
        bindings=binding_list,
    )
    user_timestamp = datetime.now(timezone.utc).isoformat()
    assistant_message_id = str(ULID())
    match_count = (
        getattr(resolution, "match_count", None)
        or len(getattr(resolution, "matches", []) or [])
        or len(binding_list)
    )
    assistant_metadata = {
        "agent_type": "redis_triage",
        "task_id": task_id,
        "message_id": assistant_message_id,
        "target_resolution_status": DISCOVERY_STATUS_TOO_MANY_MATCHES,
        "max_selectable": getattr(resolution, "max_selectable", None)
        or MULTI_TARGET_SELECTION_LIMIT,
        "match_count": match_count,
    }
    await task_manager.add_task_update(
        task_id,
        "Target discovery matched too many Redis targets for one deep triage request",
        "target_limit_exceeded",
        metadata=assistant_metadata,
    )
    messages_to_append = []
    if append_user_message:
        messages_to_append.append(
            {
                "role": "user",
                "content": user_message,
                "metadata": {"timestamp": user_timestamp},
            }
        )
    messages_to_append.append(
        {
            "message_id": assistant_message_id,
            "role": "assistant",
            "content": response_text,
            "metadata": assistant_metadata,
        },
    )
    await thread_manager.append_messages(thread_id, messages_to_append)
    result = {
        "response": response_text,
        "metadata": assistant_metadata,
        "thread_id": thread_id,
        "task_id": task_id,
        "message_id": assistant_message_id,
        "target_resolution": (
            resolution.public_dump() if hasattr(resolution, "public_dump") else None
        ),
        "turn_completed_at": datetime.now(timezone.utc).isoformat(),
    }
    completed = await _complete_task_if_open(
        task_manager=task_manager,
        task_id=task_id,
        thread_id=thread_id,
        result=result,
    )
    if not completed:
        return _build_terminal_task_result(task_id, thread_id, TaskStatus.CANCELLED)
    await task_manager._publish_stream_update(
        thread_id,
        "turn_complete",
        {"task_id": task_id, "message": "Task completed successfully"},
    )
    return result


async def _complete_turn_authorization_denied(
    *,
    task_manager: TaskManager,
    thread_manager: ThreadManager,
    thread_id: str,
    task_id: str,
    user_message: str,
    response_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Complete a turn denied by infrastructure authorization.

    Default response is IDENTICAL for 'exists-but-denied' and 'does-not-exist' so it cannot be
    used as an enumeration oracle. Callers that already know the user named the target (deep
    triage, high-confidence match) may pass an explicit `response_text` naming it.
    """
    response_text = (
        response_text
        or "You are not authorized to access the requested target, or it does not exist."
    )
    user_timestamp = datetime.now(timezone.utc).isoformat()
    assistant_message_id = str(ULID())
    assistant_metadata = {
        "agent_type": "authorization",
        "task_id": task_id,
        "message_id": assistant_message_id,
        "authorization_denied": True,
    }
    await task_manager.add_task_update(
        task_id,
        "Authorization denied for the requested target",
        "authorization_denied",
        metadata=assistant_metadata,
    )
    await thread_manager.append_messages(
        thread_id,
        [
            {"role": "user", "content": user_message, "metadata": {"timestamp": user_timestamp}},
            {
                "message_id": assistant_message_id,
                "role": "assistant",
                "content": response_text,
                "metadata": assistant_metadata,
            },
        ],
    )
    result = {
        "response": response_text,
        "metadata": assistant_metadata,
        "thread_id": thread_id,
        "task_id": task_id,
        "message_id": assistant_message_id,
        "turn_completed_at": datetime.now(timezone.utc).isoformat(),
    }
    completed = await _complete_task_if_open(
        task_manager=task_manager, task_id=task_id, thread_id=thread_id, result=result
    )
    if not completed:
        return _build_terminal_task_result(task_id, thread_id, TaskStatus.CANCELLED)
    await task_manager._publish_stream_update(
        thread_id, "turn_complete", {"task_id": task_id, "message": "Task completed successfully"}
    )
    return result


async def prepare_conversation(*, thread, thread_id, message, thread_manager):
    """Build this turn's transcript and persist the incoming message early."""
    messages = [
        {
            "role": m.role,
            "content": m.content,
            **({"metadata": m.metadata} if m.metadata else {}),
        }
        for m in thread.messages
    ]
    logger.debug(f"Loaded {len(messages)} messages from thread")

    conversation_state = {
        "messages": messages,
        "thread_id": thread_id,
    }

    # Add the new user message
    user_msg_timestamp = datetime.now(timezone.utc).isoformat()
    conversation_state["messages"].append(
        {
            "role": "user",
            "content": message,
            "timestamp": user_msg_timestamp,
        }
    )

    # Persist the new user message early so UI transcript reflects it during processing
    user_message_appended_to_thread = False
    try:
        await thread_manager.append_messages(
            thread_id,
            [
                {
                    "role": "user",
                    "content": message,
                    "metadata": {"timestamp": user_msg_timestamp},
                }
            ],
        )
        user_message_appended_to_thread = True
    except Exception as e:
        logger.warning(f"Failed to persist user message early for thread {thread_id}: {e}")

    return conversation_state, user_message_appended_to_thread


async def pending_turn_result(*, agent_response, task_manager, task_id, thread_id):
    """Return a cancellation/approval result before attempting successful completion."""
    pending_approval = _extract_pending_approval_from_response(agent_response)
    try:
        current_task_state = await task_manager.get_task_state(task_id)
    except Exception:
        logger.debug("Unable to read task state for %s after agent execution", task_id)
        current_task_state = None
    if current_task_state and current_task_state.status == TaskStatus.CANCELLED:
        logger.info("Task %s was cancelled before completion", task_id)
        return _build_terminal_task_result(task_id, thread_id, TaskStatus.CANCELLED)
    if pending_approval or (
        current_task_state and current_task_state.status == TaskStatus.AWAITING_APPROVAL
    ):
        pending_approval = pending_approval or (
            current_task_state.pending_approval.model_dump(mode="json")
            if current_task_state and current_task_state.pending_approval
            else None
        )
        result = {
            "status": TaskStatus.AWAITING_APPROVAL.value,
            "thread_id": thread_id,
            "task_id": task_id,
            "pending_approval": pending_approval,
            "resume_supported": current_task_state.resume_supported if current_task_state else True,
        }
        logger.info("Task %s is awaiting approval", task_id)
        return result

    return None


async def record_turn_qa(*, agent_response, message, thread, task_id, thread_id):
    """Citation indexing is best effort and must not block a completed answer."""
    response_text = agent_response.get("response", "")
    search_results = agent_response.get("search_results", [])
    if response_text and search_results:
        try:
            qa_manager = QAManager()
            await qa_manager.record_qa_from_search(
                question=message,
                answer=response_text,
                search_results=search_results,
                thread_id=thread_id,
                task_id=task_id,
                user_id=thread.metadata.user_id,
            )
            logger.info(f"Recorded Q&A with {len(search_results)} citations for task {task_id}")
        except Exception as e:
            logger.warning(f"Failed to record Q&A with citations: {e}")


async def update_thread_subject(*, thread, thread_id, thread_manager):
    """Populate an empty subject from the original query or first user message."""
    try:
        subj = (thread.metadata.subject or "").strip()
        if not subj or subj.lower() in {"untitled", "unknown"}:
            candidate = None
            oq = thread.context.get("original_query") if isinstance(thread.context, dict) else None
            if isinstance(oq, str) and oq.strip():
                candidate = oq.strip()
            else:
                # Find the first user message content
                for m in thread.messages:
                    if m.role == "user" and m.content.strip():
                        candidate = m.content.strip()
                        break
            if candidate:
                # Normalize to a single line and cap length
                line = candidate.splitlines()[0].strip()
                if len(line) > 80:
                    line = line[:77].rstrip() + "…"
                await thread_manager.set_thread_subject(thread_id, line)
    except Exception as e:
        logger.warning(f"Failed to set optimistic subject for thread {thread_id}: {e}")


async def persist_routed_turn_result(
    *,
    agent_response,
    conversation_state,
    thread,
    thread_id,
    task_id,
    task_manager,
    thread_manager,
    otel_trace_id=None,
    update_subject=True,
    approval_manager=None,
):
    """Save a routed answer; preserve cancellation and approval-resume cleanup order."""
    response_text = agent_response.get("response", "")
    search_results = agent_response.get("search_results", [])
    assistant_message_id = str(ULID())
    assistant_metadata = dict(agent_response.get("metadata", {}) or {})
    assistant_metadata["task_id"] = task_id
    assistant_metadata["message_id"] = assistant_message_id
    citation_groups = build_citation_group_payloads(search_results)
    conversation_state["messages"].append(
        {
            "message_id": assistant_message_id,
            "role": "assistant",
            "content": response_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": assistant_metadata,
        }
    )

    # Update thread context with new conversation state
    # Only save user/assistant/system messages - tool messages are internal to LangGraph
    # and shouldn't be persisted across turns
    clean_messages = [
        msg
        for msg in conversation_state["messages"]
        if isinstance(msg, dict) and msg.get("role") in ["user", "assistant", "system"]
    ]

    # Convert clean_messages dicts to Message objects for thread storage
    thread.messages = [
        Message(
            message_id=m.get("message_id"),
            role=m.get("role", "user"),
            content=m.get("content", ""),
            metadata={k: v for k, v in m.items() if k not in ("role", "content")} or None,
        )
        for m in clean_messages
        if m.get("content")
    ]
    thread.context["last_updated"] = datetime.now(timezone.utc).isoformat()

    if update_subject:
        await update_thread_subject(
            thread=thread, thread_id=thread_id, thread_manager=thread_manager
        )
    # Save the updated thread state to Redis
    await thread_manager._save_thread_state(thread)
    logger.info(
        f"Saved conversation history: {len(thread.messages)} user/assistant messages (filtered from {len(conversation_state['messages'])} total)"
    )

    # Set the final result on the task (not the thread - results belong on tasks)
    result = {
        "response": agent_response.get("response", ""),
        "metadata": agent_response.get("metadata", {}),
        "thread_id": thread_id,
        "task_id": task_id,
        "message_id": assistant_message_id,
        "turn_completed_at": datetime.now(timezone.utc).isoformat(),
        "citation_groups": citation_groups,
    }

    completed = await _complete_task_if_open(
        task_manager=task_manager,
        task_id=task_id,
        thread_id=thread_id,
        result=result,
    )
    if approval_manager is not None:
        await approval_manager.delete_resume_state(task_id)
    if not completed:
        return _build_terminal_task_result(task_id, thread_id, TaskStatus.CANCELLED)

    await store_response_trace(
        thread_manager=thread_manager,
        message_id=assistant_message_id,
        tool_envelopes=agent_response.get("tool_envelopes", []),
        otel_trace_id=otel_trace_id,
    )
    # Publish completion to stream for WebSocket updates
    await task_manager._publish_stream_update(
        thread_id,
        "turn_complete",
        {"task_id": task_id, "message": "Task completed successfully"},
    )

    return result


async def store_response_trace(*, thread_manager, message_id, tool_envelopes, otel_trace_id=None):
    """Persist evidence only when the answer contains tool records."""
    if tool_envelopes and message_id:
        await thread_manager.set_message_trace(
            message_id=message_id,
            tool_envelopes=tool_envelopes,
            otel_trace_id=otel_trace_id,
        )


def current_trace_id():
    """Return the active trace ID when tracing is enabled."""
    from opentelemetry import trace

    try:
        span_context = trace.get_current_span().get_span_context()
        return format(span_context.trace_id, "032x") if span_context.is_valid else None
    except Exception:
        return None


async def persist_legacy_chat_result(
    *,
    response: AgentResponse,
    result,
    task_id,
    thread_id,
    task_manager,
    thread_manager,
):
    """Keep legacy MCP result shapes while sharing message and evidence storage."""
    await task_manager.set_task_result(task_id, result)
    await task_manager.update_task_status(task_id, TaskStatus.DONE)
    message_id = str(ULID())
    await store_response_trace(
        thread_manager=thread_manager,
        message_id=message_id,
        tool_envelopes=response.tool_envelopes,
        otel_trace_id=current_trace_id(),
    )
    await thread_manager.append_messages(
        thread_id,
        [
            {
                "role": "assistant",
                "content": response.response,
                "metadata": {"task_id": task_id, "message_id": message_id, "agent": "chat"},
            }
        ],
    )
    return result
