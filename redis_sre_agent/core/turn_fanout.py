"""Run an isolated deep-triage child task for each selected target."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from langgraph.errors import GraphInterrupt
from ulid import ULID

from redis_sre_agent.agent import get_sre_agent
from redis_sre_agent.core.agent_execution import run_agent_with_progress
from redis_sre_agent.core.approvals import (
    ApprovalRequiredError,
)
from redis_sre_agent.core.progress import TaskEmitter
from redis_sre_agent.core.targets import (
    build_bound_target_scope_context,
)
from redis_sre_agent.core.tasks import TaskManager, TaskStatus
from redis_sre_agent.core.threads import ThreadManager
from redis_sre_agent.core.turn_approval import (
    _transition_task_to_awaiting_approval,
    _transition_task_to_awaiting_approval_from_interrupt,
)
from redis_sre_agent.core.turn_completion import (
    _build_terminal_task_result,
    _complete_task_if_open,
    _target_binding_metadata,
)
from redis_sre_agent.core.turn_scope import TurnScope

logger = logging.getLogger(__name__)


def _build_single_target_triage_query(*, original_query: str, binding: Any) -> str:
    """Scope a fan-out child run to one target while preserving the user's request."""
    metadata = _target_binding_metadata(binding)
    return "\n".join(
        [
            "Run the requested deep triage for this one Redis target only.",
            (
                "Target: "
                f"{metadata['display_name']} "
                f"[handle={metadata['target_handle']}, kind={metadata['target_kind']}]"
            ),
            "",
            f"Original user request: {original_query}",
        ]
    )


def _build_single_target_context(
    *,
    base_context: Dict[str, Any],
    binding: Any,
    child_task_id: str,
    parent_task_id: str,
    thread_id: str,
    child_session_id: str,
    generation: int,
) -> Dict[str, Any]:
    """Build an agent context that exposes exactly one attached target."""
    child_context = dict(base_context)
    target_handle = str(getattr(binding, "target_handle", "") or "")
    child_context.update(
        build_bound_target_scope_context(
            [binding],
            generation=generation,
            active_handle=target_handle,
        )
    )
    child_context.pop("instance_id", None)
    child_context.pop("cluster_id", None)
    child_context.pop("turn_scope", None)
    child_context.update(
        {
            "task_id": child_task_id,
            "parent_task_id": parent_task_id,
            "thread_id": thread_id,
            "session_id": child_session_id,
        }
    )
    child_scope = TurnScope.from_context(
        child_context,
        thread_id=thread_id,
        session_id=child_session_id,
    )
    child_context.update(child_scope.to_thread_context())
    child_context["turn_scope"] = child_scope.model_dump(mode="json")
    child_context.pop("instance_id", None)
    child_context.pop("cluster_id", None)
    return child_context


def _format_single_target_triage_response(*, binding: Any, response_text: str) -> str:
    """Label a child triage result with its target identity for thread display."""
    metadata = _target_binding_metadata(binding)
    target_label = metadata["display_name"] or metadata["target_handle"] or "Redis target"
    return f"## Deep triage: {target_label}\n\n{response_text}".strip()


async def _run_single_target_triage_child(
    *,
    binding: Any,
    child_task_id: str,
    parent_task_id: str,
    thread_id: str,
    thread: Any,
    task_manager: TaskManager,
    thread_manager: ThreadManager,
    base_conversation_state: Dict[str, Any],
    base_context: Dict[str, Any],
    original_query: str,
    generation: int,
) -> Dict[str, Any]:
    """Run one deep-triage child task for one target binding."""
    target_metadata = _target_binding_metadata(binding)
    target_label = target_metadata["display_name"] or target_metadata["target_handle"]
    child_session_id = f"{thread_id}:{child_task_id}"
    child_context = _build_single_target_context(
        base_context=base_context,
        binding=binding,
        child_task_id=child_task_id,
        parent_task_id=parent_task_id,
        thread_id=thread_id,
        child_session_id=child_session_id,
        generation=generation,
    )
    child_messages = [
        dict(message)
        for message in list(base_conversation_state.get("messages") or [])[:-1]
        if isinstance(message, dict)
    ]
    child_messages.append(
        {
            "role": "user",
            "content": _build_single_target_triage_query(
                original_query=original_query,
                binding=binding,
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    child_conversation_state = {
        "messages": child_messages,
        "thread_id": child_session_id,
    }
    child_emitter = TaskEmitter(task_manager=task_manager, task_id=child_task_id)

    await task_manager.update_task_status(child_task_id, TaskStatus.IN_PROGRESS)
    await task_manager.add_task_update(
        child_task_id,
        f"Starting deep triage for {target_label}",
        "agent_start",
        metadata={
            "parent_task_id": parent_task_id,
            **target_metadata,
        },
    )

    try:
        agent_response = await run_agent_with_progress(
            get_sre_agent(),
            child_conversation_state,
            child_emitter,
            thread,
            agent_context=child_context,
        )
    except GraphInterrupt as exc:
        approval_result = await _transition_task_to_awaiting_approval_from_interrupt(
            task_manager=task_manager,
            task_id=child_task_id,
            thread_id=thread_id,
            error=exc,
        )
        approval_result["parent_task_id"] = parent_task_id
        approval_result["target"] = target_metadata
        return approval_result
    except ApprovalRequiredError as exc:
        approval_result = await _transition_task_to_awaiting_approval(
            task_manager=task_manager,
            task_id=child_task_id,
            thread_id=thread_id,
            error=exc,
        )
        approval_result["parent_task_id"] = parent_task_id
        approval_result["target"] = target_metadata
        return approval_result
    except Exception as exc:
        error_message = f"Deep triage failed for {target_label}: {exc}"
        result = {
            "status": TaskStatus.FAILED.value,
            "response": error_message,
            "thread_id": thread_id,
            "task_id": child_task_id,
            "parent_task_id": parent_task_id,
            "target": target_metadata,
            "turn_completed_at": datetime.now(timezone.utc).isoformat(),
        }
        await task_manager.set_task_result(child_task_id, result)
        await task_manager.set_task_error(child_task_id, error_message)
        await task_manager.add_task_update(
            child_task_id,
            error_message,
            "error",
            metadata={"parent_task_id": parent_task_id, **target_metadata},
        )
        return result

    response_text = str(agent_response.get("response") or "")
    child_message_id = str(ULID())
    metadata = {
        "agent_type": "redis_triage",
        "parent_task_id": parent_task_id,
        "task_id": child_task_id,
        "message_id": child_message_id,
        "target": target_metadata,
    }
    result = {
        "status": TaskStatus.DONE.value,
        "response": response_text,
        "metadata": metadata,
        "thread_id": thread_id,
        "task_id": child_task_id,
        "parent_task_id": parent_task_id,
        "message_id": child_message_id,
        "target": target_metadata,
        "turn_completed_at": datetime.now(timezone.utc).isoformat(),
    }

    completed = await _complete_task_if_open(
        task_manager=task_manager,
        task_id=child_task_id,
        thread_id=thread_id,
        result=result,
    )
    if not completed:
        return {
            **_build_terminal_task_result(child_task_id, thread_id, TaskStatus.CANCELLED),
            "parent_task_id": parent_task_id,
            "target": target_metadata,
        }

    await thread_manager.append_messages(
        thread_id,
        [
            {
                "message_id": child_message_id,
                "role": "assistant",
                "content": _format_single_target_triage_response(
                    binding=binding,
                    response_text=response_text,
                ),
                "metadata": metadata,
            }
        ],
    )

    tool_envelopes = agent_response.get("tool_envelopes", [])
    if tool_envelopes:
        await thread_manager.set_message_trace(
            message_id=child_message_id,
            tool_envelopes=tool_envelopes,
            otel_trace_id=None,
        )

    await task_manager._publish_stream_update(
        thread_id,
        "turn_complete",
        {
            "task_id": child_task_id,
            "parent_task_id": parent_task_id,
            "message": f"Deep triage completed for {target_label}",
            "target": target_metadata,
        },
    )
    return result


async def _run_multi_target_deep_triage_fanout(
    *,
    bindings: List[Any],
    task_id: str,
    thread_id: str,
    thread: Any,
    task_manager: TaskManager,
    thread_manager: ThreadManager,
    conversation_state: Dict[str, Any],
    routing_context: Dict[str, Any],
    original_query: str,
    generation: int,
) -> Dict[str, Any]:
    """Fan out a multi-target deep-triage turn into one child task per target."""
    child_specs: List[Dict[str, Any]] = []
    for binding in bindings:
        target_metadata = _target_binding_metadata(binding)
        target_label = target_metadata["display_name"] or target_metadata["target_handle"]
        child_task_id = await task_manager.create_task(
            thread_id=thread_id,
            user_id=thread.metadata.user_id,
            subject=f"Deep triage: {target_label}",
        )
        child_specs.append(
            {
                "task_id": child_task_id,
                "binding": binding,
                "target": target_metadata,
            }
        )

    await task_manager.add_task_update(
        task_id,
        f"Starting deep triage fan-out for {len(child_specs)} targets",
        "triage_fanout_start",
        metadata={
            "child_task_ids": [spec["task_id"] for spec in child_specs],
            "target_count": len(child_specs),
            "targets": [spec["target"] for spec in child_specs],
        },
    )

    child_tasks = [
        asyncio.create_task(
            _run_single_target_triage_child(
                binding=spec["binding"],
                child_task_id=spec["task_id"],
                parent_task_id=task_id,
                thread_id=thread_id,
                thread=thread,
                task_manager=task_manager,
                thread_manager=thread_manager,
                base_conversation_state=conversation_state,
                base_context=routing_context,
                original_query=original_query,
                generation=generation,
            )
        )
        for spec in child_specs
    ]

    child_results: List[Dict[str, Any]] = []
    try:
        for child_task in asyncio.as_completed(child_tasks):
            child_result = await child_task
            child_results.append(child_result)
            target = child_result.get("target") or {}
            target_label = target.get("display_name") or target.get("target_handle") or "target"
            status = str(child_result.get("status") or TaskStatus.DONE.value)
            update_type = (
                "triage_fanout_child_failed"
                if status == TaskStatus.FAILED.value
                else "triage_fanout_child_complete"
            )
            await task_manager.add_task_update(
                task_id,
                f"Deep triage {status} for {target_label}",
                update_type,
                metadata={
                    "child_task_id": child_result.get("task_id"),
                    "message_id": child_result.get("message_id"),
                    "status": status,
                    "target": target,
                },
            )
    except BaseException:
        for child_task in child_tasks:
            if not child_task.done():
                child_task.cancel()
        await asyncio.gather(*child_tasks, return_exceptions=True)
        raise

    failed_count = sum(
        1 for child_result in child_results if child_result.get("status") == TaskStatus.FAILED.value
    )
    awaiting_approval_count = sum(
        1
        for child_result in child_results
        if child_result.get("status") == TaskStatus.AWAITING_APPROVAL.value
    )
    response = (
        f"Deep triage fan-out completed for {len(child_results)} targets. "
        "Individual target reports were emitted as child task results."
    )
    if failed_count or awaiting_approval_count:
        response += (
            f" Failed targets: {failed_count}. Awaiting approval: {awaiting_approval_count}."
        )

    result = {
        "response": response,
        "metadata": {
            "agent_type": "redis_triage",
            "fanout": True,
            "target_count": len(bindings),
            "failed_count": failed_count,
            "awaiting_approval_count": awaiting_approval_count,
        },
        "thread_id": thread_id,
        "task_id": task_id,
        "child_task_ids": [spec["task_id"] for spec in child_specs],
        "target_results": [
            {
                "task_id": child_result.get("task_id"),
                "message_id": child_result.get("message_id"),
                "status": child_result.get("status"),
                "target": child_result.get("target"),
            }
            for child_result in child_results
        ],
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
        {"task_id": task_id, "message": "Multi-target deep triage fan-out completed"},
    )
    return result
