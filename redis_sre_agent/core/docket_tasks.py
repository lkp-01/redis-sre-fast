"""Docket task definitions for SRE operations."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from docket import ConcurrencyLimit, Docket, Perpetual, Retry
from langgraph.errors import GraphInterrupt
from ulid import ULID

from redis_sre_agent.agent.chat_agent import get_chat_agent
from redis_sre_agent.core.agent_execution import _thread_messages_to_conversation_history
from redis_sre_agent.core.approvals import (
    ApprovalDecisionType,
    ApprovalRequiredError,
)
from redis_sre_agent.core.clusters import get_cluster_by_id
from redis_sre_agent.core.config import Settings, settings
from redis_sre_agent.core.instances import (
    get_instance_by_id,
)
from redis_sre_agent.core.knowledge_helpers import (
    ingest_sre_document_helper,
    search_knowledge_base_helper,
)
from redis_sre_agent.core.llm_token_usage import LLMTokenLimitExceededError
from redis_sre_agent.core.progress import TaskEmitter
from redis_sre_agent.core.qa import QAManager
from redis_sre_agent.core.redis import (
    get_redis_client,
)
from redis_sre_agent.core.tasks import TaskManager, TaskStatus
from redis_sre_agent.core.threads import ThreadManager
from redis_sre_agent.core.turn_approval import (
    _resume_task_after_approval_impl,
    _transition_task_to_awaiting_approval,
    _transition_task_to_awaiting_approval_from_interrupt,
)
from redis_sre_agent.core.turn_approval import (
    validate_task_resume_request as validate_task_resume_request,
)
from redis_sre_agent.core.turn_completion import (
    _extract_pending_approval_from_response,
    persist_legacy_chat_result,
)
from redis_sre_agent.core.turn_runner import run_agent_turn as _process_agent_turn_impl

logger = logging.getLogger(__name__)

# SRE-specific task registry
SRE_TASK_COLLECTION = []


def sre_task(func):
    """Decorator to register SRE tasks."""
    SRE_TASK_COLLECTION.append(func)
    return func


@sre_task
async def search_knowledge_base(
    query: str,
    category: Optional[str] = None,
    doc_type: Optional[str] = None,
    limit: int = 5,
    distance_threshold: Optional[float] = None,
    retry: Retry = Retry(attempts=2, delay=timedelta(seconds=2)),
) -> Dict[str, Any]:
    """
    Search SRE knowledge base and runbooks (background task wrapper).

    This is a Docket task wrapper around the core helper function.
    It adds retry logic and task tracking for background execution.

    Args:
        query: Search query text
        category: Optional category filter (incident, maintenance, monitoring, etc.)
        doc_type: Optional document type filter
        limit: Maximum number of results
        distance_threshold: Optional cosine distance threshold. If provided, overrides backend default.
        retry: Retry configuration

    Returns:
        Dictionary with search results
    """
    try:
        kwargs = {"query": query, "limit": limit}
        if category is not None:
            kwargs["category"] = category
        if doc_type is not None:
            kwargs["doc_type"] = doc_type
        if distance_threshold is not None:
            kwargs["distance_threshold"] = distance_threshold
        result = await search_knowledge_base_helper(**kwargs)
        # Ensure a task_id is present for callers/tests expecting it
        try:
            from ulid import ULID

            result.setdefault("task_id", str(ULID()))
        except Exception:
            # Best-effort; absence shouldn't break callers
            result.setdefault("task_id", "task")
        return result
    except Exception as e:
        logger.error(f"Knowledge search failed (attempt {retry.attempt}): {e}")
        raise


@sre_task
async def ingest_sre_document(
    title: str,
    content: str,
    source: str,
    category: str = "general",
    severity: str = "info",
    doc_type: Optional[str] = None,
    product_labels: Optional[List[str]] = None,
    retry: Retry = Retry(attempts=3, delay=timedelta(seconds=2)),
) -> Dict[str, Any]:
    """
    Ingest a document into the SRE knowledge base (background task wrapper).

    This is a Docket task wrapper around the core helper function.
    It adds retry logic and task tracking for background execution.

    Args:
        title: Document title
        content: Document content
        source: Source system or file
        category: Document category (incident, runbook, monitoring, etc.)
        severity: Severity level (info, warning, critical)
        doc_type: Optional document type
        product_labels: Optional list of product labels
        retry: Retry configuration

    Returns:
        Dictionary with ingestion result
    """
    try:
        return await ingest_sre_document_helper(
            title=title,
            content=content,
            source=source,
            category=category,
            severity=severity,
            doc_type=doc_type,
            product_labels=product_labels,
        )
    except Exception as e:
        logger.error(f"Document ingestion failed (attempt {retry.attempt}): {e}")
        raise


@sre_task
async def embed_qa_record(
    qa_id: str,
    retry: Retry = Retry(attempts=3, delay=timedelta(seconds=2)),
) -> Dict[str, Any]:
    """
    Generate embeddings for a Q&A record out-of-band.

    This Docket task fetches the Q&A record, generates embeddings for the
    question and answer using the configured vectorizer, and updates the
    record with the vectors. This allows Q&A recording to remain fast
    while embeddings are computed asynchronously.

    Args:
        qa_id: The ID of the QuestionAnswer record to embed
        retry: Retry configuration

    Returns:
        Dictionary with status and qa_id
    """
    from redis_sre_agent.core.redis import get_vectorizer

    try:
        # Get the Q&A record
        qa_manager = QAManager()
        qa = await qa_manager.get_qa(qa_id)

        if qa is None:
            logger.warning(f"Q&A record {qa_id} not found for embedding")
            return {"status": "error", "error": f"Q&A record {qa_id} not found", "qa_id": qa_id}

        # Generate embeddings
        vectorizer = get_vectorizer()
        question_vector = await vectorizer.aembed(qa.question, as_buffer=True)
        answer_vector = await vectorizer.aembed(qa.answer, as_buffer=True)

        # Update the record with vectors
        await qa_manager.update_vectors(
            qa_id=qa_id,
            question_vector=question_vector,
            answer_vector=answer_vector,
        )

        logger.info(f"Embedded Q&A record {qa_id}")
        return {"status": "success", "qa_id": qa_id}

    except Exception as e:
        logger.error(f"Q&A embedding failed for {qa_id} (attempt {retry.attempt}): {e}")
        raise


@sre_task
async def process_chat_turn(
    query: str,
    task_id: str,
    thread_id: str,
    instance_id: Optional[str] = None,
    cluster_id: Optional[str] = None,
    user_id: Optional[str] = None,
    exclude_mcp_categories: Optional[List[str]] = None,
    retry: Retry = Retry(attempts=2, delay=timedelta(seconds=2)),
) -> Dict[str, Any]:
    """Docket task wrapper for the chat turn.

    This task carries no authenticated principal (its only caller, MCP, is disabled under
    authz), so clear the worker auth token and reset it in a finally — a reused worker context
    must not leak a prior task's principal into the guarded loaders this turn hits (fail-closed
    by construction, matching process_agent_turn / resume_task_after_approval).
    """
    from redis_sre_agent.core.authorization import reset_auth_token

    _authz_token = await _set_worker_auth_token(None)
    try:
        return await _process_chat_turn_impl(
            query=query,
            task_id=task_id,
            thread_id=thread_id,
            instance_id=instance_id,
            cluster_id=cluster_id,
            user_id=user_id,
            exclude_mcp_categories=exclude_mcp_categories,
        )
    finally:
        if _authz_token is not None:
            reset_auth_token(_authz_token)


async def _process_chat_turn_impl(
    query: str,
    task_id: str,
    thread_id: str,
    instance_id: Optional[str] = None,
    cluster_id: Optional[str] = None,
    user_id: Optional[str] = None,
    exclude_mcp_categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Process a chat query using the ChatAgent (background task).

    This runs the lightweight ChatAgent for quick Q&A about Redis instances.
    Notifications are emitted to the task, and the result is stored on both
    the task and the thread.

    Args:
        query: User's question
        task_id: Task ID for notifications and result storage
        thread_id: Thread ID for conversation context and result storage
        instance_id: Optional Redis instance ID
        cluster_id: Optional Redis cluster ID
        user_id: Optional user ID for tracking
        exclude_mcp_categories: Optional list of MCP tool category names to exclude.
            Valid values: "metrics", "logs", "tickets", "repos", "traces",
            "diagnostics", "knowledge", "utilities".

    Returns:
        Dictionary with the chat response
    """
    from redis_sre_agent.agent.chat_agent import ChatAgent
    from redis_sre_agent.tools.models import ToolCapability

    logger.info(f"Processing chat turn for task {task_id}")

    redis_client = get_redis_client()
    task_manager = TaskManager(redis_client=redis_client)
    thread_manager = ThreadManager(redis_client=redis_client)

    # Mark task as in progress
    await task_manager.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    try:
        # Convert string category names to ToolCapability enums
        mcp_categories: Optional[List[ToolCapability]] = None
        if exclude_mcp_categories:
            mcp_categories = []
            for cat_name in exclude_mcp_categories:
                try:
                    mcp_categories.append(ToolCapability(cat_name.lower()))
                except ValueError:
                    logger.warning(f"Unknown MCP category to exclude: {cat_name}")

        # Create task emitter for notifications
        emitter = TaskEmitter(task_manager=task_manager, task_id=task_id)

        if instance_id and cluster_id:
            raise ValueError("Please provide only one of instance_id or cluster_id")

        # Get Redis instance if specified
        redis_instance = None
        if instance_id:
            redis_instance = await get_instance_by_id(instance_id)
            if not redis_instance:
                raise ValueError(f"Instance not found: {instance_id}")

        redis_cluster = None
        if cluster_id:
            redis_cluster = await get_cluster_by_id(cluster_id)
            if not redis_cluster:
                raise ValueError(f"Cluster not found: {cluster_id}")

        # Run chat agent
        agent = ChatAgent(
            redis_instance=redis_instance,
            redis_cluster=redis_cluster,
            progress_emitter=emitter,
            exclude_mcp_categories=mcp_categories,
        )
        agent_context = {"task_id": task_id}
        if instance_id:
            agent_context["instance_id"] = instance_id
        if cluster_id:
            agent_context["cluster_id"] = cluster_id
        response = await agent.process_query(
            query=query,
            session_id=thread_id,
            user_id=user_id,
            context=agent_context,
            progress_emitter=emitter,
        )

        pending_approval = _extract_pending_approval_from_response(response)
        current_task_state = await task_manager.get_task_state(task_id)
        if pending_approval or (
            current_task_state and current_task_state.status == TaskStatus.AWAITING_APPROVAL
        ):
            result = {
                "status": TaskStatus.AWAITING_APPROVAL.value,
                "task_id": task_id,
                "thread_id": thread_id,
                "pending_approval": pending_approval
                or (
                    current_task_state.pending_approval.model_dump(mode="json")
                    if current_task_state and current_task_state.pending_approval
                    else None
                ),
                "resume_supported": current_task_state.resume_supported
                if current_task_state
                else True,
            }
            return result

        # Store result on task (convert AgentResponse to dict for JSON serialization)
        result = {
            "response": response.model_dump(),
            "instance_id": instance_id,
            "cluster_id": cluster_id,
        }
        return await persist_legacy_chat_result(
            response=response, result=result, task_id=task_id, thread_id=thread_id,
            task_manager=task_manager, thread_manager=thread_manager,
        )

    except GraphInterrupt as exc:
        logger.info("Chat turn paused awaiting approval for task %s", task_id)
        return await _transition_task_to_awaiting_approval_from_interrupt(
            task_manager=task_manager,
            task_id=task_id,
            thread_id=thread_id,
            error=exc,
        )

    except ApprovalRequiredError as exc:
        logger.info("Chat turn paused awaiting approval for task %s", task_id)
        return await _transition_task_to_awaiting_approval(
            task_manager=task_manager,
            task_id=task_id,
            thread_id=thread_id,
            error=exc,
        )

    except Exception as e:
        logger.error(f"Chat turn failed: {e}")
        await task_manager.set_task_error(task_id, str(e))
        raise


@sre_task
async def process_knowledge_query(
    query: str,
    task_id: str,
    thread_id: str,
    user_id: Optional[str] = None,
    retry: Retry = Retry(attempts=2, delay=timedelta(seconds=2)),
) -> Dict[str, Any]:
    """
    Process a knowledge query using the chat agent compatibility path.

    This routes onto the default chat agent with no explicit Redis scope.
    Notifications are emitted to the task, and the result is stored on both
    the task and the thread.

    Args:
        query: User's question about SRE practices or Redis
        task_id: Task ID for notifications and result storage
        thread_id: Thread ID for conversation context and result storage
        user_id: Optional user ID for tracking
        retry: Retry configuration

    Returns:
        Dictionary with the knowledge agent response
    """
    logger.info(f"Processing knowledge query for task {task_id}")

    redis_client = get_redis_client()
    task_manager = TaskManager(redis_client=redis_client)
    thread_manager = ThreadManager(redis_client=redis_client)

    # Mark task as in progress
    await task_manager.update_task_status(task_id, TaskStatus.IN_PROGRESS)

    try:
        # Create task emitter for notifications
        emitter = TaskEmitter(task_manager=task_manager, task_id=task_id)

        conversation_history = None
        thread = await thread_manager.get_thread(thread_id)
        if thread and thread.messages:
            history_messages = list(thread.messages)
            if (
                history_messages
                and history_messages[-1].role == "user"
                and history_messages[-1].content == query
            ):
                history_messages = history_messages[:-1]
            conversation_history = _thread_messages_to_conversation_history(history_messages)

        # Run chat agent without explicit target scope. This preserves the
        # legacy entrypoint while keeping runtime behavior on the two-agent model.
        agent = get_chat_agent()
        chat_max_iterations = min(int(settings.max_iterations or 15), 10)
        response = await agent.process_query(
            query=query,
            session_id=thread_id,
            user_id=user_id,
            max_iterations=chat_max_iterations,
            context={"task_id": task_id},
            progress_emitter=emitter,
            conversation_history=conversation_history,
        )

        pending_approval = _extract_pending_approval_from_response(response)
        current_task_state = await task_manager.get_task_state(task_id)
        if pending_approval or (
            current_task_state and current_task_state.status == TaskStatus.AWAITING_APPROVAL
        ):
            return {
                "status": TaskStatus.AWAITING_APPROVAL.value,
                "task_id": task_id,
                "thread_id": thread_id,
                "pending_approval": pending_approval
                or (
                    current_task_state.pending_approval.model_dump(mode="json")
                    if current_task_state and current_task_state.pending_approval
                    else None
                ),
                "resume_supported": current_task_state.resume_supported
                if current_task_state
                else True,
            }

        # Store result on task (convert AgentResponse to dict for JSON serialization)
        result = {
            "response": response.model_dump(),
        }
        return await persist_legacy_chat_result(
            response=response, result=result, task_id=task_id, thread_id=thread_id,
            task_manager=task_manager, thread_manager=thread_manager,
        )

    except GraphInterrupt as exc:
        logger.info("Knowledge query paused awaiting approval for task %s", task_id)
        return await _transition_task_to_awaiting_approval_from_interrupt(
            task_manager=task_manager,
            task_id=task_id,
            thread_id=thread_id,
            error=exc,
        )
    except ApprovalRequiredError as exc:
        logger.info("Knowledge query paused awaiting approval for task %s", task_id)
        return await _transition_task_to_awaiting_approval(
            task_manager=task_manager,
            task_id=task_id,
            thread_id=thread_id,
            error=exc,
        )
    except LLMTokenLimitExceededError as exc:
        logger.error("Knowledge query exceeded LLM context token budget: %s", exc)
        await task_manager.set_task_error(task_id, str(exc))
        raise
    except Exception as e:
        logger.error(f"Knowledge query failed: {e}")
        await task_manager.set_task_error(task_id, str(e))
        raise


@sre_task
async def process_pipeline_operation(
    operation: str,
    task_id: str,
    thread_id: str,
    batch_date: Optional[str] = None,
    artifacts_path: str = "./artifacts",
    scrapers: Optional[List[str]] = None,
    latest_only: bool = False,
    docs_path: str = "./redis-docs",
    source_dir: str = "source_documents",
    prepare_only: bool = False,
    keep_days: int = 30,
    retry: Retry = Retry(attempts=2, delay=timedelta(seconds=2)),
) -> Dict[str, Any]:
    """Run a task-backed pipeline operation and persist its result."""
    from redis_sre_agent.core.pipeline_execution_helpers import run_pipeline_operation_helper

    logger.info("Processing pipeline operation %s for task %s", operation, task_id)

    redis_client = get_redis_client()
    task_manager = TaskManager(redis_client=redis_client)

    await task_manager.update_task_status(task_id, TaskStatus.IN_PROGRESS)

    try:
        emitter = TaskEmitter(task_manager=task_manager, task_id=task_id)
        result = await run_pipeline_operation_helper(
            operation=operation,
            batch_date=batch_date,
            artifacts_path=artifacts_path,
            scrapers=scrapers,
            latest_only=latest_only,
            docs_path=docs_path,
            source_dir=source_dir,
            prepare_only=prepare_only,
            keep_days=keep_days,
            progress_emitter=emitter,
        )
        await task_manager.set_task_result(task_id, result)
        await task_manager.update_task_status(task_id, TaskStatus.DONE)
        return result
    except Exception as e:
        logger.error(
            "Pipeline operation %s failed for task %s (attempt %s): %s",
            operation,
            task_id,
            retry.attempt,
            e,
        )
        await task_manager.set_task_error(task_id, str(e))
        raise


@sre_task
async def scheduler_task(
    perpetual: Perpetual = Perpetual(every=timedelta(seconds=30), automatic=True),
    concurrency: ConcurrencyLimit = ConcurrencyLimit(max_concurrent=1),
    retry: Retry = Retry(attempts=3, delay=timedelta(seconds=5)),
) -> Dict[str, Any]:
    """
    Scheduler task that runs every 30 seconds using Perpetual with automatic=True.

    With automatic=True, Docket automatically starts and reschedules this task when
    a worker is running. No manual startup or rescheduling is needed.

    The ConcurrencyLimit ensures only ONE instance of this task runs at a time,
    preventing multiple workers or rapid rescheduling from creating duplicates.

    This task:
    1. Queries Redis for schedules that need to run based on current time
    2. Submits tasks to Docket with deduplication keys
    3. Updates schedule next_run_at times
    """
    # Scheduling is disabled while infrastructure authorization is enabled (this phase):
    # a pre-existing schedule must NOT fire unscoped. No-op so nothing is enqueued.
    if settings.infrastructure_authorization_enabled:
        logger.info(
            "scheduler_task skipped: infrastructure authorization enabled (scheduling disabled)"
        )
        return {"status": "skipped", "reason": "infrastructure_authorization_enabled"}
    try:
        logger.info("Running scheduler task")
        current_time = datetime.now(timezone.utc)

        # Import schedule storage functions
        from ..core.schedules import (
            find_schedules_needing_runs,
            update_schedule_last_run,
            update_schedule_next_run,
        )

        # Find schedules that need runs
        schedules_needing_runs = await find_schedules_needing_runs(current_time)
        logger.info(f"Found {len(schedules_needing_runs)} schedules needing runs")

        if not schedules_needing_runs:
            logger.debug("No schedules need runs at this time")
            return {
                "task_id": str(ULID()),
                "submitted_tasks": 0,
                "timestamp": current_time.isoformat(),
                "status": "completed",
            }

        submitted_tasks = 0

        # Get Docket instance
        async with Docket(url=await get_redis_url(), name="sre_docket") as docket:
            for schedule in schedules_needing_runs:
                try:
                    schedule_id = schedule["id"]

                    # Calculate when this task should actually run
                    # Use the next_run_at time from the schedule
                    if schedule.get("next_run_at"):
                        scheduled_time = datetime.fromisoformat(
                            schedule["next_run_at"].replace("Z", "+00:00")
                        )
                    else:
                        # Fallback to current time if no next_run_at
                        scheduled_time = current_time

                    # Create a thread for this scheduled task
                    redis_client = get_redis_client()
                    thread_manager = ThreadManager(redis_client=redis_client)

                    # Prepare context for the scheduled run
                    run_context = {
                        "schedule_id": schedule_id,
                        "schedule_name": schedule["name"],
                        "automated": True,
                        "original_query": schedule["instructions"],
                        "scheduled_at": scheduled_time.isoformat(),
                    }

                    if schedule.get("redis_instance_id"):
                        run_context["instance_id"] = schedule["redis_instance_id"]

                    # Create thread for the scheduled run
                    thread_id = await thread_manager.create_thread(
                        user_id="scheduler",
                        session_id=f"schedule_{schedule_id}_{scheduled_time.strftime('%Y%m%d_%H%M')}",
                        initial_context=run_context,
                        tags=["automated", "scheduled"],
                    )

                    # Set subject for scheduled tasks to the schedule name for clarity
                    await thread_manager.set_thread_subject(thread_id, schedule["name"])

                    # Create a unique deduplication key for this schedule + time slot
                    # This ensures we don't create duplicate tasks for the same schedule at the same time
                    time_slot = scheduled_time.strftime("%Y%m%d_%H%M")  # Round to minute precision
                    task_key = f"schedule_{schedule_id}_{time_slot}"

                    # Use Redis-based deduplication to prevent race conditions
                    redis_client = await get_redis_client()
                    dedup_key = f"sre_task_dedup:{task_key}"

                    # Try to set the deduplication key with expiration (5 minutes)
                    # This will only succeed if the key doesn't already exist
                    task_submitted = await redis_client.set(dedup_key, "submitted", ex=300, nx=True)

                    if task_submitted:
                        # We successfully claimed this task slot, submit to Docket
                        task_func = docket.add(
                            process_agent_turn, when=scheduled_time, key=task_key
                        )
                        agent_task_id = await task_func(
                            thread_id=thread_id,
                            message=schedule["instructions"],
                            context=run_context,
                        )
                        logger.info(
                            f"Submitted agent task {agent_task_id} for schedule {schedule_id} at {scheduled_time} with key {task_key}"
                        )
                        submitted_tasks += 1
                    else:
                        # Another scheduler task already submitted this task
                        logger.debug(
                            f"Agent task for schedule {schedule_id} at {scheduled_time} already submitted by another scheduler (key: {task_key})"
                        )

                    # Update last run time for both successful and skipped tasks
                    await update_schedule_last_run(schedule_id, scheduled_time)

                except Exception as e:
                    logger.error(f"Failed to process schedule {schedule_id}: {e}")

                # Calculate and update next run time regardless of success/failure
                try:
                    from ..core.schedules import Schedule

                    schedule_obj = Schedule(**schedule)
                    next_run = schedule_obj.calculate_next_run()
                    await update_schedule_next_run(schedule_id, next_run)
                    logger.debug(f"Updated next run time for schedule {schedule_id} to {next_run}")
                except Exception as e:
                    logger.error(f"Failed to update next run time for schedule {schedule_id}: {e}")

        result = {
            "task_id": str(ULID()),
            "processed_schedules": len(schedules_needing_runs),
            "submitted_tasks": submitted_tasks,
            "timestamp": current_time.isoformat(),
            "status": "completed",
        }

        logger.info(
            f"Scheduler task completed: processed {len(schedules_needing_runs)} schedules, submitted {submitted_tasks} tasks"
        )

        # Note: With automatic=True, Docket will automatically reschedule this task
        # No manual rescheduling needed

        return result

    except Exception as e:
        logger.error(f"Scheduler task failed (attempt {retry.attempt}): {e}")
        raise


async def get_redis_url() -> str:
    """Get Redis URL for Docket."""
    return settings.redis_url.get_secret_value()


async def register_sre_tasks() -> None:
    """Register all SRE tasks with Docket."""
    try:
        async with Docket(url=await get_redis_url(), name="sre_docket") as docket:
            # Register all SRE tasks
            for task in SRE_TASK_COLLECTION:
                docket.register(task)

            logger.info(f"Registered {len(SRE_TASK_COLLECTION)} SRE tasks with Docket")
    except Exception as e:
        logger.error(f"Failed to register SRE tasks: {e}")
        raise


@sre_task
async def resume_task_after_approval(
    task_id: str,
    approval_id: str,
    decision: ApprovalDecisionType | str,
    decision_by: Optional[str] = None,
    decision_comment: Optional[str] = None,
    authz_bearer: Optional[str] = None,
    redis_client=None,
    concurrency: ConcurrencyLimit = ConcurrencyLimit(
        "task_id", max_concurrent=1, scope="task_approval_resume"
    ),
    retry: Retry = Retry(attempts=2, delay=timedelta(seconds=2)),
) -> Dict[str, Any]:
    """Set the authz principal (the approver's bearer) around the resume impl, then reset it.

    Resume re-runs a previously-gated tool against a target; access may have been revoked
    during the approval wait, so the principal must be live here. Reset-in-finally is required
    so a reused worker context cannot leak this principal into a later task (e.g. process_chat_turn
    must stay fail-closed)."""
    from redis_sre_agent.core.authorization import reset_auth_token

    _authz_token = await _set_worker_auth_token({"_authz_bearer": authz_bearer})
    try:
        return await _resume_task_after_approval_impl(
            task_id=task_id,
            approval_id=approval_id,
            decision=decision,
            decision_by=decision_by,
            decision_comment=decision_comment,
            redis_client=redis_client,
        )
    finally:
        if _authz_token is not None:
            reset_auth_token(_authz_token)


async def _set_worker_auth_token(context: Optional[Dict[str, Any]]):
    """Resolve + set the validated auth TOKEN for a worker turn.

    A worker turn runs after the request returned, so identity comes from the bearer persisted
    in the turn context (captured at create_task / resume). This is THE point where deferred
    turns get scoped — set the token here and every guarded loader/resolver the turn hits
    enforces against it. Fan-out children (asyncio.create_task) inherit this context.

    We RE-VALIDATE the bearer here (authn: signature/iss/aud/exp) before setting it, so authz
    never runs on an unverified/expired token.

    - authz off               -> no-op (returns None).
    - valid bearer            -> set that token (validation catches expiry/spoofing).
    - invalid/expired/no bearer -> None (fail closed). The only tokenless agent path is MCP,
      which is disabled under authz.
    Returns the ContextVar reset token (or None) to reset in finally.
    """
    if not settings.infrastructure_authorization_enabled:
        return None
    from redis_sre_agent.core import auth as _core_auth
    from redis_sre_agent.core.authorization import set_auth_token

    bearer = (context or {}).get("_authz_bearer")
    token = None
    if bearer:
        try:
            await _core_auth.validate_token(bearer)  # authn; raises on invalid/expired
            token = bearer
        except Exception:
            token = None  # expired/invalid/discovery-down -> fail closed
    return set_auth_token(token)


@sre_task
async def process_agent_turn(
    thread_id: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
    concurrency: ConcurrencyLimit = ConcurrencyLimit(
        "thread_id", max_concurrent=1, scope="thread_turns"
    ),
    retry: Retry = Retry(attempts=3, delay=timedelta(seconds=5)),
) -> Dict[str, Any]:
    """Docket-managed wrapper around the in-process agent turn implementation."""
    from redis_sre_agent.core.authorization import reset_auth_token

    _authz_token = await _set_worker_auth_token(context)
    try:
        return await _process_agent_turn_impl(
            thread_id=thread_id,
            message=message,
            context=context,
            task_id=task_id,
        )
    finally:
        if _authz_token is not None:
            reset_auth_token(_authz_token)


async def test_task_system(config: Optional[Settings] = None) -> bool:
    """Test if the task system is working.

    Args:
        config: Optional Settings instance for dependency injection.
                If not provided, uses get_redis_url() for backwards compatibility
                with unit tests that patch that function.

    Returns:
        True if the task system is working, False otherwise.
    """
    try:
        # Use injected config if provided, otherwise call get_redis_url()
        # for backwards compatibility with unit tests that patch it
        if config is not None:
            redis_url = config.redis_url.get_secret_value()
        else:
            redis_url = await get_redis_url()

        # Try to connect to Docket
        async with Docket(url=redis_url, name="sre_docket"):
            # Simple connectivity test
            return True
    except Exception as e:
        logger.error(f"Task system test failed: {e}")
        return False
