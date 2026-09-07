"""Approval validation, checkpoint recovery and resumed turn execution."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from langgraph.errors import GraphInterrupt
from ulid import ULID

from redis_sre_agent.agent import get_sre_agent
from redis_sre_agent.core.approvals import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalManager,
    ApprovalRecord,
    ApprovalRequiredError,
    ApprovalStatus,
)
from redis_sre_agent.core.clusters import get_cluster_by_id
from redis_sre_agent.core.config import settings
from redis_sre_agent.core.encryption import encrypt_secret, get_secret_value
from redis_sre_agent.core.instances import (
    RedisInstance,
    add_session_instance,
    get_session_instances,
)
from redis_sre_agent.core.progress import TaskEmitter
from redis_sre_agent.core.redis import (
    get_redis_client,
)
from redis_sre_agent.core.tasks import TaskManager, TaskStatus
from redis_sre_agent.core.threads import ThreadManager
from redis_sre_agent.core.turn_completion import (
    _build_terminal_task_result,
    _complete_task_if_open,
    _complete_turn_authorization_denied,
    persist_routed_turn_result,
)
from redis_sre_agent.core.turn_targeting import _resolve_instance_for_thread

logger = logging.getLogger(__name__)


def _extract_pending_approval_from_interrupt(error: GraphInterrupt) -> Optional[Dict[str, Any]]:
    """Return the approval payload embedded in a LangGraph interrupt."""
    if not error.args:
        return None

    interrupts = error.args[0]
    if not isinstance(interrupts, (list, tuple)):
        interrupts = [interrupts]

    for item in interrupts:
        payload = getattr(item, "value", None)
        if isinstance(payload, dict) and payload.get("kind") == "approval_required":
            return payload
    return None


def _serialize_session_instance_snapshot(instance: RedisInstance) -> Dict[str, Any]:
    """Persist a staged session instance in resume state with encrypted secrets."""
    payload = instance.model_dump(mode="json")
    if payload.get("connection_url"):
        payload["connection_url"] = encrypt_secret(payload["connection_url"])
    return payload


def _deserialize_session_instance_snapshot(payload: Dict[str, Any]) -> RedisInstance:
    """Restore a staged session instance snapshot from resume state."""
    data = dict(payload)
    if data.get("connection_url"):
        data["connection_url"] = get_secret_value(data["connection_url"])
    return RedisInstance(**data)


async def _persist_staged_session_instance_for_resume(
    *,
    task_id: str,
    thread_id: str,
    redis_client: Any,
) -> None:
    """Store the current thread-scoped instance in durable resume state when needed."""
    approval_manager = ApprovalManager(redis_client=redis_client)
    resume_state = await approval_manager.get_resume_state(task_id)
    if not resume_state:
        return

    thread = await ThreadManager(redis_client=redis_client).get_thread(thread_id)
    thread_context = thread.context if thread else {}
    instance_id = str(thread_context.get("instance_id") or "").strip() or None
    if not instance_id:
        return

    for session_instance in await get_session_instances(thread_id):
        if session_instance.id != instance_id:
            continue
        snapshot = _serialize_session_instance_snapshot(session_instance)
        await approval_manager.save_resume_state(
            resume_state.model_copy(update={"staged_session_instance": snapshot})
        )
        return


async def _resolve_instance_for_resume(
    *,
    instance_id: Optional[str],
    thread_id: str,
    resume_state: Any,
) -> Optional[RedisInstance]:
    """Resolve a task target, falling back to a durable staged-instance snapshot."""
    instance = await _resolve_instance_for_thread(instance_id, thread_id)
    if instance is not None:
        return instance

    snapshot = getattr(resume_state, "staged_session_instance", None)
    if not snapshot:
        return None

    restored = _deserialize_session_instance_snapshot(snapshot)
    if instance_id and restored.id != instance_id:
        return None

    if not await add_session_instance(thread_id, restored):
        raise ValueError("Failed to restore staged session instance for resume")
    return restored


async def _transition_task_to_awaiting_approval(
    *,
    task_manager: TaskManager,
    task_id: str,
    thread_id: str,
    error: ApprovalRequiredError,
) -> Dict[str, Any]:
    """Persist task state for a paused turn waiting on human approval."""
    pending_approval = error.pending_approval
    approval_record = error.approval_record

    await task_manager.update_task_status(task_id, TaskStatus.AWAITING_APPROVAL)
    await task_manager.set_pending_approval(task_id, pending_approval)
    await task_manager.set_resume_supported(task_id, True)
    await task_manager.add_task_update(
        task_id,
        error.decision.message or "Approval required before continuing task execution.",
        "pending_approval",
        metadata={
            "approval_id": approval_record.approval_id if approval_record else None,
            "interrupt_id": approval_record.interrupt_id if approval_record else None,
            "tool_name": error.decision.tool_name,
            "tool_args_preview": (
                approval_record.tool_args_preview if approval_record is not None else {}
            ),
            "target_handles": approval_record.target_handles if approval_record is not None else [],
            "expires_at": approval_record.expires_at if approval_record is not None else None,
        },
    )

    result = {
        "status": TaskStatus.AWAITING_APPROVAL.value,
        "thread_id": thread_id,
        "task_id": task_id,
        "resume_supported": True,
        "pending_approval": (
            pending_approval.model_dump(mode="json") if pending_approval is not None else None
        ),
        "approval_id": approval_record.approval_id if approval_record is not None else None,
        "interrupt_id": approval_record.interrupt_id if approval_record is not None else None,
        "tool_name": error.decision.tool_name,
    }
    await task_manager.set_task_result(task_id, result)
    try:
        await _persist_staged_session_instance_for_resume(
            task_id=task_id,
            thread_id=thread_id,
            redis_client=task_manager._redis,
        )
    except Exception:
        logger.debug("Failed to persist staged session instance snapshot for task %s", task_id)
    try:
        await task_manager._publish_stream_update(
            thread_id,
            "awaiting_approval",
            {
                "task_id": task_id,
                "message": "Task is awaiting approval",
                "pending_approval": result["pending_approval"] or {},
            },
        )
    except Exception:
        logger.debug("Failed to publish awaiting_approval update for task %s", task_id)
    return result


def _approval_is_expired(record: ApprovalRecord) -> bool:
    """Return True when an approval has passed its expiry timestamp."""

    if not record.expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))
    except Exception:
        return False
    return expires_at <= datetime.now(timezone.utc)


def _normalize_approval_decision(
    decision: ApprovalDecisionType | str,
) -> ApprovalDecisionType:
    """Normalize caller input into an approval decision enum."""

    if isinstance(decision, ApprovalDecisionType):
        return decision

    normalized = str(decision or "").strip().lower()
    if normalized == ApprovalDecisionType.APPROVED.value:
        return ApprovalDecisionType.APPROVED
    if normalized == ApprovalDecisionType.REJECTED.value:
        return ApprovalDecisionType.REJECTED
    raise ValueError(f"Unsupported approval decision: {decision}")


def _build_resume_payload(
    *,
    approval_record: ApprovalRecord,
    decision: ApprovalDecision,
) -> Dict[str, Any]:
    """Build the resume payload passed back into LangGraph."""

    return {
        "approval_id": approval_record.approval_id,
        "interrupt_id": approval_record.interrupt_id,
        "decision": decision.decision.value,
        "decision_at": decision.decision_at,
        "decision_by": decision.decision_by,
        "decision_comment": decision.decision_comment,
        "action_hash": approval_record.action_hash,
        "tool_name": approval_record.tool_name,
    }


async def _load_resume_context(
    *,
    task_id: str,
    approval_id: str,
    decision: ApprovalDecisionType | str,
    decision_by: Optional[str],
    decision_comment: Optional[str],
    task_state: Optional[Any] = None,
    task_manager: TaskManager,
    thread_manager: ThreadManager,
    approval_manager: ApprovalManager,
) -> tuple[Any, Any, Any, ApprovalRecord, ApprovalDecision]:
    """Load and validate the persisted state needed to resume an approval pause."""

    task_state = task_state or await task_manager.get_task_state(task_id)
    if not task_state:
        raise ValueError(f"Task {task_id} not found")
    if task_state.status not in {TaskStatus.AWAITING_APPROVAL, TaskStatus.IN_PROGRESS}:
        raise ValueError(f"Task {task_id} is not awaiting approval")

    thread_id = task_state.thread_id
    thread = await thread_manager.get_thread(thread_id)
    if not thread:
        raise ValueError(f"Thread {thread_id} not found for task {task_id}")

    resume_state = await approval_manager.get_resume_state(task_id)
    if not resume_state:
        raise ValueError(f"Task {task_id} is missing resume state")

    approval_record = await approval_manager.get_approval(approval_id)
    if not approval_record:
        raise ValueError(f"Approval {approval_id} not found")
    if approval_record.task_id != task_id:
        raise ValueError(f"Approval {approval_id} does not belong to task {task_id}")

    pending_summary = getattr(task_state, "pending_approval", None)
    if pending_summary is not None:
        if pending_summary.approval_id != approval_id:
            raise ValueError(
                f"Task {task_id} is waiting on approval {pending_summary.approval_id}, not {approval_id}"
            )
        if pending_summary.interrupt_id != approval_record.interrupt_id:
            raise ValueError(
                "Approval interrupt does not match the current pending approval for this task"
            )

    if resume_state.pending_approval_id and resume_state.pending_approval_id != approval_id:
        raise ValueError(
            f"Task {task_id} is waiting on approval {resume_state.pending_approval_id}, not {approval_id}"
        )
    if (
        resume_state.pending_interrupt_id
        and resume_state.pending_interrupt_id != approval_record.interrupt_id
    ):
        raise ValueError("Approval interrupt does not match the current resume checkpoint")
    if (
        approval_record.graph_thread_id
        and resume_state.graph_thread_id
        and approval_record.graph_thread_id != resume_state.graph_thread_id
    ):
        raise ValueError("Approval graph thread does not match the current resume checkpoint")
    if (
        approval_record.graph_type
        and resume_state.graph_type
        and approval_record.graph_type != resume_state.graph_type
    ):
        raise ValueError("Approval graph type does not match the current resume checkpoint")

    if _approval_is_expired(approval_record) and approval_record.status is ApprovalStatus.PENDING:
        approval_record = await approval_manager.expire_approval(approval_id) or approval_record
    if approval_record.status is ApprovalStatus.EXPIRED:
        raise ValueError(f"Approval {approval_id} has expired")

    normalized_decision = _normalize_approval_decision(decision)
    decision_model = ApprovalDecision(
        decision=normalized_decision,
        decision_by=decision_by,
        decision_comment=decision_comment,
    )
    return task_state, thread, resume_state, approval_record, decision_model


async def validate_task_resume_request(
    *,
    task_id: str,
    approval_id: str,
    decision: ApprovalDecisionType | str,
    decision_by: Optional[str] = None,
    decision_comment: Optional[str] = None,
    redis_client=None,
) -> None:
    """Validate that a task can be resumed for the requested approval decision."""

    redis_client = redis_client or get_redis_client()
    task_manager = TaskManager(redis_client=redis_client)
    task_state = await task_manager.get_task_state(task_id)
    if not task_state:
        raise ValueError(f"Task {task_id} not found")
    if task_state.status != TaskStatus.AWAITING_APPROVAL:
        return

    await _load_resume_context(
        task_id=task_id,
        approval_id=approval_id,
        decision=decision,
        decision_by=decision_by,
        decision_comment=decision_comment,
        task_state=task_state,
        task_manager=task_manager,
        thread_manager=ThreadManager(redis_client=redis_client),
        approval_manager=ApprovalManager(redis_client=redis_client),
    )


def _build_awaiting_approval_result(
    *,
    task_id: str,
    thread_id: str,
    task_state: Any,
) -> Dict[str, Any]:
    """Build a serialized awaiting-approval result payload from TaskState."""

    pending_approval = (
        task_state.pending_approval.model_dump(mode="json")
        if task_state and task_state.pending_approval
        else None
    )
    return {
        "status": TaskStatus.AWAITING_APPROVAL.value,
        "thread_id": thread_id,
        "task_id": task_id,
        "pending_approval": pending_approval,
        "resume_supported": bool(task_state.resume_supported) if task_state else True,
    }


async def _transition_task_to_awaiting_approval_from_interrupt(
    *,
    task_manager: TaskManager,
    task_id: str,
    thread_id: str,
    error: GraphInterrupt,
) -> Dict[str, Any]:
    """Persist task state when LangGraph returns an approval interrupt."""
    task_state = await task_manager.get_task_state(task_id)
    pending_approval = task_state.pending_approval if task_state is not None else None
    payload = _extract_pending_approval_from_interrupt(error) or {}
    payload_pending = payload.get("pending_approval")
    if pending_approval is None and isinstance(payload_pending, dict):
        try:
            from redis_sre_agent.core.approvals import PendingApprovalSummary

            pending_approval = PendingApprovalSummary(**payload_pending)
        except Exception:
            pending_approval = None

    await task_manager.set_pending_approval(task_id, pending_approval)
    await task_manager.set_resume_supported(task_id, True)
    await task_manager.add_task_update(
        task_id,
        str(payload.get("message") or "Approval required before continuing task execution."),
        "pending_approval",
        metadata={"pending_approval": payload_pending or {}},
    )

    result = {
        "status": TaskStatus.AWAITING_APPROVAL.value,
        "thread_id": thread_id,
        "task_id": task_id,
        "resume_supported": True,
        "pending_approval": (
            pending_approval.model_dump(mode="json") if pending_approval is not None else None
        ),
        "approval_id": payload.get("approval_id")
        or (pending_approval.approval_id if pending_approval is not None else None),
        "interrupt_id": payload.get("interrupt_id")
        or (pending_approval.interrupt_id if pending_approval is not None else None),
        "tool_name": payload.get("tool_name")
        or (pending_approval.tool_name if pending_approval is not None else None),
    }
    await task_manager.set_task_result(task_id, result)
    await task_manager.update_task_status(task_id, TaskStatus.AWAITING_APPROVAL)
    try:
        await _persist_staged_session_instance_for_resume(
            task_id=task_id,
            thread_id=thread_id,
            redis_client=task_manager._redis,
        )
    except Exception:
        logger.debug("Failed to persist staged session instance snapshot for task %s", task_id)
    try:
        await task_manager._publish_stream_update(
            thread_id,
            "awaiting_approval",
            {
                "task_id": task_id,
                "message": "Task is awaiting approval",
                "pending_approval": result["pending_approval"] or {},
            },
        )
    except Exception:
        logger.debug("Failed to publish awaiting_approval update for task %s", task_id)
    return result


async def _resume_task_after_approval_impl(
    task_id: str,
    approval_id: str,
    decision: ApprovalDecisionType | str,
    decision_by: Optional[str] = None,
    decision_comment: Optional[str] = None,
    redis_client=None,
) -> Dict[str, Any]:
    """Resume a paused task after recording a human approval decision."""

    redis_client = redis_client or get_redis_client()
    task_manager = TaskManager(redis_client=redis_client)
    thread_manager = ThreadManager(redis_client=redis_client)
    approval_manager = ApprovalManager(redis_client=redis_client)

    task_state = await task_manager.get_task_state(task_id)
    if not task_state:
        raise ValueError(f"Task {task_id} not found")
    if task_state.status not in {TaskStatus.AWAITING_APPROVAL, TaskStatus.IN_PROGRESS}:
        if task_state.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            await approval_manager.delete_resume_state(task_id)
        return {
            "task_id": task_id,
            "thread_id": task_state.thread_id,
            "status": task_state.status.value,
            "result": task_state.result,
        }

    (
        task_state,
        thread,
        resume_state,
        approval_record,
        decision_model,
    ) = await _load_resume_context(
        task_id=task_id,
        approval_id=approval_id,
        decision=decision,
        decision_by=decision_by,
        decision_comment=decision_comment,
        task_state=task_state,
        task_manager=task_manager,
        thread_manager=thread_manager,
        approval_manager=approval_manager,
    )
    thread_id = task_state.thread_id
    normalized_decision = decision_model.decision

    async def _record_resume_failure(error: Exception) -> None:
        error_message = f"Approval resume failed: {str(error)}"
        logger.error("Approval resume failed for task %s: %s", task_id, error)
        await task_manager.set_task_error(task_id, error_message)
        await task_manager.add_task_update(task_id, f"Error: {error_message}", "error")
        await thread_manager.append_messages(
            thread_id,
            [
                {
                    "role": "assistant",
                    "content": (
                        f"I encountered an error while resuming your approved request: {str(error)}"
                    ),
                }
            ],
        )

    if approval_record.decision is None:
        approval_record = (
            await approval_manager.record_decision(
                approval_id,
                decision_model,
            )
            or approval_record
        )
    elif approval_record.decision.decision != normalized_decision:
        raise ValueError(
            f"Approval {approval_id} was already decided as {approval_record.decision.decision.value}"
        )

    await task_manager.set_pending_approval(task_id, None)
    await task_manager.set_resume_supported(task_id, True)
    await task_manager.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    await task_manager.add_task_update(
        task_id,
        f"Approval {normalized_decision.value} for {approval_record.tool_name}",
        "approval_decision",
        metadata={
            "approval_id": approval_record.approval_id,
            "interrupt_id": approval_record.interrupt_id,
            "tool_name": approval_record.tool_name,
            "decision": normalized_decision.value,
            "decision_by": decision_by,
            "decision_comment": decision_comment,
        },
    )
    await approval_manager.save_resume_state(
        resume_state.model_copy(
            update={
                "waiting_reason": "resuming",
                "resume_count": resume_state.resume_count + 1,
                "pending_approval_id": approval_record.approval_id,
                "pending_interrupt_id": approval_record.interrupt_id,
            }
        )
    )

    resume_payload = _build_resume_payload(
        approval_record=approval_record,
        decision=decision_model,
    )
    from redis_sre_agent.agent.checkpointing import persist_approval_wait_state

    progress_emitter = TaskEmitter(task_manager=task_manager, task_id=task_id)
    resume_context = dict(thread.context or {})
    resume_context["task_id"] = task_id
    resume_context["thread_id"] = thread_id

    if resume_state.graph_type == "chat":
        from redis_sre_agent.agent.chat_agent import ChatAgent
        from redis_sre_agent.tools.models import ToolCapability

        instance_id = str(resume_context.get("instance_id") or "").strip() or None
        cluster_id = str(resume_context.get("cluster_id") or "").strip() or None
        redis_instance = await _resolve_instance_for_resume(
            instance_id=instance_id,
            thread_id=thread_id,
            resume_state=resume_state,
        )
        redis_cluster = await get_cluster_by_id(cluster_id) if cluster_id else None

        # Authorization: the gated tool re-runs against this target; if access was revoked
        # during the approval wait, the guarded loaders return None -> deny, do not resume.
        if settings.infrastructure_authorization_enabled and (
            (instance_id and redis_instance is None) or (cluster_id and redis_cluster is None)
        ):
            return await _complete_turn_authorization_denied(
                task_manager=task_manager,
                thread_manager=thread_manager,
                thread_id=thread_id,
                task_id=task_id,
                user_message=str(resume_context.get("original_query") or "resume"),
            )

        excluded_categories = resume_context.get("exclude_mcp_categories") or []
        mcp_categories = []
        for cat_name in excluded_categories if isinstance(excluded_categories, list) else []:
            try:
                mcp_categories.append(ToolCapability(str(cat_name).lower()))
            except ValueError:
                logger.warning("Unknown MCP category to exclude during resume: %s", cat_name)

        chat_agent = ChatAgent(
            redis_instance=redis_instance,
            redis_cluster=redis_cluster,
            progress_emitter=progress_emitter,
            exclude_mcp_categories=mcp_categories or None,
        )
        try:
            response = await chat_agent.resume_query(
                session_id=thread.metadata.session_id or thread_id,
                user_id=thread.metadata.user_id,
                context=resume_context,
                progress_emitter=progress_emitter,
                resume_payload=resume_payload,
            )
        except GraphInterrupt as exc:
            result = await _transition_task_to_awaiting_approval_from_interrupt(
                task_manager=task_manager,
                task_id=task_id,
                thread_id=thread_id,
                error=exc,
            )
            current_task_state = await task_manager.get_task_state(task_id)
            await persist_approval_wait_state(
                task_id=task_id,
                pending_approval=current_task_state.pending_approval
                if current_task_state
                else None,
            )
            return result
        except ApprovalRequiredError as exc:
            result = await _transition_task_to_awaiting_approval(
                task_manager=task_manager,
                task_id=task_id,
                thread_id=thread_id,
                error=exc,
            )
            await persist_approval_wait_state(
                task_id=task_id,
                pending_approval=exc.pending_approval,
            )
            return result
        except Exception as exc:
            await _record_resume_failure(exc)
            raise

        current_task_state = await task_manager.get_task_state(task_id)
        if current_task_state and current_task_state.status == TaskStatus.CANCELLED:
            await approval_manager.delete_resume_state(task_id)
            return _build_terminal_task_result(task_id, thread_id, TaskStatus.CANCELLED)
        if current_task_state and current_task_state.status == TaskStatus.AWAITING_APPROVAL:
            result = _build_awaiting_approval_result(
                task_id=task_id,
                thread_id=thread_id,
                task_state=current_task_state,
            )
            await task_manager.set_task_result(task_id, result)
            await persist_approval_wait_state(
                task_id=task_id,
                pending_approval=current_task_state.pending_approval,
            )
            return result

        result = {
            "response": response.model_dump() if hasattr(response, "model_dump") else response,
            "instance_id": instance_id,
            "cluster_id": cluster_id,
        }
        completed = await _complete_task_if_open(
            task_manager=task_manager,
            task_id=task_id,
            thread_id=thread_id,
            result=result,
        )
        if not completed:
            await approval_manager.delete_resume_state(task_id)
            return _build_terminal_task_result(task_id, thread_id, TaskStatus.CANCELLED)
        await approval_manager.delete_resume_state(task_id)

        message_id = str(ULID())
        tool_envelopes = response.tool_envelopes if hasattr(response, "tool_envelopes") else []
        if tool_envelopes:
            await thread_manager.set_message_trace(
                message_id=message_id,
                tool_envelopes=tool_envelopes,
                otel_trace_id=None,
            )

        response_text = response.response if hasattr(response, "response") else str(response)
        await thread_manager.append_messages(
            thread_id,
            [
                {
                    "role": "assistant",
                    "content": response_text,
                    "metadata": {
                        "task_id": task_id,
                        "message_id": message_id,
                        "agent": "chat",
                    },
                }
            ],
        )
        return result

    if resume_state.graph_type != "redis_triage":
        raise ValueError(f"Unsupported resume graph type: {resume_state.graph_type}")

    instance_id = str(resume_context.get("instance_id") or "").strip() or None
    cluster_id = str(resume_context.get("cluster_id") or "").strip() or None
    target_instance = await _resolve_instance_for_resume(
        instance_id=instance_id,
        thread_id=thread_id,
        resume_state=resume_state,
    )
    target_cluster = await get_cluster_by_id(cluster_id) if cluster_id else None

    # Authorization: same live re-check for the deep-triage resume branch.
    if settings.infrastructure_authorization_enabled and (
        (instance_id and target_instance is None) or (cluster_id and target_cluster is None)
    ):
        return await _complete_turn_authorization_denied(
            task_manager=task_manager,
            thread_manager=thread_manager,
            thread_id=thread_id,
            task_id=task_id,
            user_message=str(resume_context.get("original_query") or "resume"),
        )

    agent = get_sre_agent(
        redis_instance=target_instance,
        redis_cluster=target_cluster,
    )
    try:
        agent_response_obj = await agent.resume_query(
            session_id=thread.metadata.session_id or thread_id,
            user_id=thread.metadata.user_id,
            context=resume_context,
            progress_emitter=progress_emitter,
            resume_payload=resume_payload,
        )
    except GraphInterrupt as exc:
        result = await _transition_task_to_awaiting_approval_from_interrupt(
            task_manager=task_manager,
            task_id=task_id,
            thread_id=thread_id,
            error=exc,
        )
        current_task_state = await task_manager.get_task_state(task_id)
        await persist_approval_wait_state(
            task_id=task_id,
            pending_approval=current_task_state.pending_approval if current_task_state else None,
        )
        return result
    except ApprovalRequiredError as exc:
        result = await _transition_task_to_awaiting_approval(
            task_manager=task_manager,
            task_id=task_id,
            thread_id=thread_id,
            error=exc,
        )
        await persist_approval_wait_state(
            task_id=task_id,
            pending_approval=exc.pending_approval,
        )
        return result
    except Exception as exc:
        await _record_resume_failure(exc)
        raise
    agent_response = {
        "response": agent_response_obj.response,
        "search_results": agent_response_obj.search_results,
        "tool_envelopes": agent_response_obj.tool_envelopes,
        "metadata": {"agent_type": "redis_triage"},
    }

    current_task_state = await task_manager.get_task_state(task_id)
    if current_task_state and current_task_state.status == TaskStatus.CANCELLED:
        await approval_manager.delete_resume_state(task_id)
        return _build_terminal_task_result(task_id, thread_id, TaskStatus.CANCELLED)
    if current_task_state and current_task_state.status == TaskStatus.AWAITING_APPROVAL:
        result = _build_awaiting_approval_result(
            task_id=task_id,
            thread_id=thread_id,
            task_state=current_task_state,
        )
        await task_manager.set_task_result(task_id, result)
        await persist_approval_wait_state(
            task_id=task_id,
            pending_approval=current_task_state.pending_approval,
        )
        return result

    conversation_state = {
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                **({"metadata": m.metadata} if m.metadata else {}),
            }
            for m in thread.messages
        ],
        "thread_id": thread_id,
    }
    return await persist_routed_turn_result(
        agent_response=agent_response,
        conversation_state=conversation_state,
        thread=thread,
        thread_id=thread_id,
        task_id=task_id,
        task_manager=task_manager,
        thread_manager=thread_manager,
        update_subject=False,
        approval_manager=approval_manager,
    )
