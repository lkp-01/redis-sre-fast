"""The routed conversation turn: prepare, execute, then persist.

Docket owns scheduling and retries. This module owns the order of one turn;
target resolution, agent invocation, completion and recovery each have a named
boundary. Read ``run_agent_turn`` first when following a background query.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.errors import GraphInterrupt
from opentelemetry import trace

from redis_sre_agent.agent.router import AgentType, route_to_appropriate_agent
from redis_sre_agent.core.agent_execution import execute_selected_agent, select_turn_agent
from redis_sre_agent.core.approvals import ApprovalRequiredError
from redis_sre_agent.core.progress import TaskEmitter
from redis_sre_agent.core.redis import get_redis_client
from redis_sre_agent.core.targets import get_target_bindings_from_context
from redis_sre_agent.core.tasks import TaskManager, TaskStatus
from redis_sre_agent.core.threads import ThreadManager
from redis_sre_agent.core.turn_approval import (
    _transition_task_to_awaiting_approval,
    _transition_task_to_awaiting_approval_from_interrupt,
)
from redis_sre_agent.core.turn_completion import (
    _complete_deep_triage_target_limit_response,
    _complete_turn_authorization_denied,
    current_trace_id,
    pending_turn_result,
    persist_routed_turn_result,
    prepare_conversation,
    record_turn_qa,
)
from redis_sre_agent.core.turn_fanout import _run_multi_target_deep_triage_fanout
from redis_sre_agent.core.turn_targeting import (
    PreparedTarget,
    authorize_named_triage_targets,
    discover_triage_targets,
    explicit_target_is_allowed,
    prepare_target_scope,
)
from redis_sre_agent.targets.contracts import MULTI_TARGET_SELECTION_LIMIT

logger = logging.getLogger(__name__)


async def run_agent_turn(
    thread_id: str,
    message: str,
    context: dict[str, Any] | None = None,
    task_id: str | None = None,
    redis_client=None,
) -> dict[str, Any]:
    """Process one query, retaining the public Docket entrypoint's result format."""
    redis_client = redis_client or get_redis_client()
    thread_manager = ThreadManager(redis_client=redis_client)
    task_manager = TaskManager(redis_client=redis_client)
    with trace.get_tracer(__name__).start_as_current_span(
        "agent.turn",
        attributes={
            "thread.id": thread_id,
            "message.len": len(message or ""),
            "agent": "sre_agent",
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            thread = await thread_manager.get_thread(thread_id)
            if task_id is None:
                task_id = await task_manager.create_task(
                    thread_id=thread_id,
                    user_id=thread.metadata.user_id if thread else None,
                )
            await task_manager.update_task_status(task_id, TaskStatus.IN_PROGRESS)
            await task_manager.add_task_update(task_id, f"Started task {task_id}", "task_start")
            if not thread:
                raise ValueError(f"Thread {thread_id} not found")

            if not await explicit_target_is_allowed(context):
                return await _complete_turn_authorization_denied(
                    task_manager=task_manager,
                    thread_manager=thread_manager,
                    thread_id=thread_id,
                    task_id=task_id,
                    user_message=message,
                )
            target = await prepare_target_scope(
                thread_id=thread_id,
                task_id=task_id,
                message=message,
                context=context,
                thread=thread,
                task_manager=task_manager,
                thread_manager=thread_manager,
            )
            agent_type = await select_agent_type(message, context, target)
            denied = await authorize_named_triage_targets(
                agent_type=agent_type,
                message=message,
                thread=thread,
                task_manager=task_manager,
                thread_manager=thread_manager,
                task_id=task_id,
                thread_id=thread_id,
            )
            if denied is not None:
                return denied
            discovery_result = await discover_triage_targets(
                target=target,
                agent_type=agent_type,
                message=message,
                thread=thread,
                task_manager=task_manager,
                thread_manager=thread_manager,
                task_id=task_id,
                thread_id=thread_id,
            )
            if discovery_result is not None:
                return discovery_result

            conversation, user_persisted = await prepare_conversation(
                thread=thread,
                thread_id=thread_id,
                message=message,
                thread_manager=thread_manager,
            )
            fanout_result = await run_triage_fanout_if_needed(
                agent_type=agent_type,
                target=target,
                message=message,
                thread=thread,
                task_id=task_id,
                thread_id=thread_id,
                task_manager=task_manager,
                thread_manager=thread_manager,
                conversation=conversation,
                user_persisted=user_persisted,
            )
            if fanout_result is not None:
                return fanout_result

            agent = await select_turn_agent(
                agent_type=agent_type, target=target, thread_id=thread_id
            )
            response = await execute_selected_agent(
                agent=agent,
                agent_type=agent_type,
                message=message,
                thread=thread,
                thread_id=thread_id,
                task_id=task_id,
                target=target,
                conversation_state=conversation,
                progress_emitter=TaskEmitter(task_manager=task_manager, task_id=task_id),
                task_manager=task_manager,
            )
            pending = await pending_turn_result(
                agent_response=response,
                task_manager=task_manager,
                task_id=task_id,
                thread_id=thread_id,
            )
            if pending is not None:
                return pending
            await record_turn_qa(
                agent_response=response,
                message=message,
                thread=thread,
                task_id=task_id,
                thread_id=thread_id,
            )
            return await persist_routed_turn_result(
                agent_response=response,
                conversation_state=conversation,
                thread=thread,
                thread_id=thread_id,
                task_id=task_id,
                task_manager=task_manager,
                thread_manager=thread_manager,
                otel_trace_id=current_trace_id(),
            )
        except GraphInterrupt as exc:
            return await _transition_task_to_awaiting_approval_from_interrupt(
                task_manager=task_manager,
                task_id=task_id,
                thread_id=thread_id,
                error=exc,
            )
        except ApprovalRequiredError as exc:
            return await _transition_task_to_awaiting_approval(
                task_manager=task_manager,
                task_id=task_id,
                thread_id=thread_id,
                error=exc,
            )
        except Exception as exc:
            span.record_exception(exc)
            await record_turn_failure(
                error=exc,
                task_manager=task_manager,
                thread_manager=thread_manager,
                task_id=task_id,
                thread_id=thread_id,
            )
            raise


async def select_agent_type(
    message: str,
    context: dict[str, Any] | None,
    target: PreparedTarget,
) -> AgentType:
    """Normalize historical mode names at the turn boundary."""
    explicit_modes = {
        "triage": AgentType.REDIS_TRIAGE,
        "chat": AgentType.REDIS_CHAT,
        "knowledge": AgentType.KNOWLEDGE_ONLY,
    }
    requested = (context or {}).get("requested_agent_type")
    if requested in explicit_modes:
        return explicit_modes[requested]
    return await route_to_appropriate_agent(
        query=message, context=target.context, user_preferences=None
    )


async def run_triage_fanout_if_needed(
    *,
    agent_type: AgentType,
    target: PreparedTarget,
    message,
    thread,
    task_id,
    thread_id,
    task_manager,
    thread_manager,
    conversation,
    user_persisted,
) -> dict[str, Any] | None:
    """Keep multi-target scheduling out of the single-target agent invocation."""
    bindings = target.scope.bindings or get_target_bindings_from_context(target.context)
    if agent_type != AgentType.REDIS_TRIAGE or len(bindings) <= 1:
        return None
    if len(bindings) > MULTI_TARGET_SELECTION_LIMIT:
        return await _complete_deep_triage_target_limit_response(
            task_manager=task_manager,
            thread_manager=thread_manager,
            thread_id=thread_id,
            task_id=task_id,
            user_message=message,
            bindings=bindings,
            append_user_message=not user_persisted,
        )
    await task_manager.add_task_update(
        task_id,
        "Processing query with per-target deep triage fan-out",
        "agent_processing",
        metadata={"target_count": len(bindings)},
    )
    return await _run_multi_target_deep_triage_fanout(
        bindings=bindings,
        task_id=task_id,
        thread_id=thread_id,
        thread=thread,
        task_manager=task_manager,
        thread_manager=thread_manager,
        conversation_state=conversation,
        routing_context=target.context,
        original_query=message,
        generation=int(
            target.context.get("target_toolset_generation") or target.scope.toolset_generation or 0
        ),
    )


async def record_turn_failure(*, error, task_manager, thread_manager, task_id, thread_id):
    """Report an execution exception to both task status and the conversation."""
    error_message = f"Agent turn failed: {error}"
    logger.error("Turn processing failed for thread %s: %s", thread_id, error)
    await task_manager.set_task_error(task_id, error_message)
    await task_manager.add_task_update(task_id, f"Error: {error_message}", "error")
    await thread_manager.append_messages(
        thread_id,
        [
            {
                "role": "assistant",
                "content": f"I encountered an error while processing your request: {error}",
            }
        ],
    )
