"""Resolve and authorize the target of a conversation turn."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ulid import ULID

from redis_sre_agent.agent.langgraph_agent import (
    _extract_instance_details_from_message,
)
from redis_sre_agent.agent.router import AgentType
from redis_sre_agent.core.clusters import get_cluster_by_id
from redis_sre_agent.core.config import settings
from redis_sre_agent.core.instances import (
    RedisInstance,
    RedisInstanceType,
    add_session_instance,
    get_instance_by_id,
    get_session_instances,
)
from redis_sre_agent.core.targets import (
    get_attached_target_handles_from_context,
)
from redis_sre_agent.core.threads import Thread
from redis_sre_agent.core.turn_completion import (
    _complete_deep_triage_target_limit_response,
    _complete_turn_authorization_denied,
)
from redis_sre_agent.core.turn_scope import TurnScope
from redis_sre_agent.targets.contracts import (
    DISCOVERY_STATUS_TOO_MANY_MATCHES,
)

logger = logging.getLogger(__name__)


@dataclass
class PreparedTarget:
    """The resolved target state shared by routing and agent construction."""

    context: Dict[str, Any]
    scope: TurnScope
    instance_id: Optional[str]
    cluster_id: Optional[str]
    staged_instance: Optional[RedisInstance]
    explicit_scope: bool
    attached_handles: List[str]


async def explicit_target_is_allowed(context: Dict[str, Any] | None) -> bool:
    """Validate the selected endpoint before persisting a scope change."""
    context = context or {}
    instance_id = context.get("instance_id")
    cluster_id = context.get("cluster_id")
    if instance_id and cluster_id:
        raise ValueError("Please provide only one of instance_id or cluster_id")
    if not settings.infrastructure_authorization_enabled or not (instance_id or cluster_id):
        return True
    from redis_sre_agent.core.instances import get_instance_by_id

    resolved = (
        await get_instance_by_id(instance_id)
        if instance_id
        else await get_cluster_by_id(cluster_id)
    )
    return resolved is not None


def materialize_turn_scope(
    routing_payload: Dict[str, Any],
    *,
    thread_id: str,
    thread: Thread,
) -> TurnScope:
    """Normalize target hints and copy the resulting scope into the routing context."""
    scope = TurnScope.from_context(
        routing_payload,
        thread_id=thread_id,
        session_id=thread.metadata.session_id or thread_id,
        seed_hints={
            key: routing_payload[key]
            for key in ("instance_id", "cluster_id")
            if routing_payload.get(key)
        },
    )
    routing_payload.update(scope.to_thread_context())
    if scope.scope_kind == "target_bindings":
        routing_payload.pop("instance_id", None)
        routing_payload.pop("cluster_id", None)
    routing_payload["turn_scope"] = scope.model_dump(mode="json")
    return scope


async def prepare_target_scope(
    *, thread_id, task_id, message, context, thread, task_manager, thread_manager
) -> PreparedTarget:
    """Prepare scope after ``explicit_target_is_allowed`` validates the client selection."""
    instance_id_from_client = context.get("instance_id") if context else None
    cluster_id_from_client = context.get("cluster_id") if context else None
    instance_id_from_thread = thread.context.get("instance_id")
    cluster_id_from_thread = thread.context.get("cluster_id")
    attached_target_handles_from_thread = get_attached_target_handles_from_context(thread.context)

    def _clear_attached_scope(target_context: Dict[str, Any]) -> None:
        target_context["attached_target_handles"] = []
        target_context["target_bindings"] = []
        target_context["target_toolset_generation"] = 0
        target_context.pop("turn_scope", None)

    # Determine active targets
    active_instance_id = None
    active_cluster_id = None
    staged_thread_instance: Optional[RedisInstance] = None

    if instance_id_from_client:
        # Client provided instance_id - use it and update thread
        active_instance_id = instance_id_from_client
        logger.info(
            f"Using instance_id from client: {active_instance_id} (will update thread context)"
        )

        # Update thread context with new instance_id and clear cluster scope
        await thread_manager.update_thread_context(
            thread_id,
            {
                "instance_id": active_instance_id,
                "cluster_id": "",
                "attached_target_handles": [],
                "target_bindings": [],
                "target_toolset_generation": 0,
                "turn_scope": "",
            },
            merge=True,
        )
        _clear_attached_scope(thread.context)
        await task_manager.add_task_update(
            task_id,
            f"Using Redis instance: {active_instance_id}",
            "instance_context",
        )

    elif cluster_id_from_client:
        # Client provided cluster_id - use it and update thread
        active_cluster_id = cluster_id_from_client
        logger.info(
            f"Using cluster_id from client: {active_cluster_id} (will update thread context)"
        )
        await thread_manager.update_thread_context(
            thread_id,
            {
                "cluster_id": active_cluster_id,
                "instance_id": "",
                "attached_target_handles": [],
                "target_bindings": [],
                "target_toolset_generation": 0,
                "turn_scope": "",
            },
            merge=True,
        )
        _clear_attached_scope(thread.context)
        await task_manager.add_task_update(
            task_id,
            f"Using Redis cluster: {active_cluster_id}",
            "cluster_context",
        )

    elif instance_id_from_thread and not attached_target_handles_from_thread:
        # No instance_id from client, but we have one saved in thread
        active_instance_id = instance_id_from_thread
        logger.info(f"Using instance_id from thread context: {active_instance_id}")
        await task_manager.add_task_update(
            task_id,
            f"Continuing with Redis instance: {active_instance_id}",
            "instance_context",
        )

    elif cluster_id_from_thread and not attached_target_handles_from_thread:
        # No explicit target from client, but cluster is saved on thread
        active_cluster_id = cluster_id_from_thread
        logger.info(f"Using cluster_id from thread context: {active_cluster_id}")
        await task_manager.add_task_update(
            task_id,
            f"Continuing with Redis cluster: {active_cluster_id}",
            "cluster_context",
        )

    else:
        # No instance_id from client or thread - attempt to create one from message
        logger.info("No instance_id found, attempting to extract from user message")

        # Try to extract connection details from the message
        instance_details = _extract_instance_details_from_message(message)

        if instance_details:
            # User provided connection details - stage a thread-scoped instance
            try:
                logger.info("Staging session instance from user-provided connection details")
                new_instance = await _stage_session_instance_from_message(
                    thread_id=thread_id,
                    thread_user_id=thread.metadata.user_id,
                    instance_details=instance_details,
                )

                active_instance_id = new_instance.id
                staged_thread_instance = new_instance
                logger.info(
                    "Staged session instance: %s (%s)",
                    new_instance.name,
                    active_instance_id,
                )

                # Save instance_id to thread context
                await thread_manager.update_thread_context(
                    thread_id,
                    {
                        "instance_id": active_instance_id,
                        "cluster_id": "",
                        "attached_target_handles": [],
                        "target_bindings": [],
                        "target_toolset_generation": 0,
                        "turn_scope": "",
                    },
                    merge=True,
                )
                _clear_attached_scope(thread.context)
                await task_manager.add_task_update(
                    task_id,
                    (
                        "Using provided Redis connection details for this thread: "
                        f"{new_instance.name} ({active_instance_id})"
                    ),
                    "instance_context",
                )

            except Exception as e:
                logger.warning(f"Failed to stage session instance from user details: {e}")
                await task_manager.add_task_update(
                    task_id,
                    f"Could not stage provided Redis details for this thread: {str(e)}",
                    "instance_error",
                )
                # Continue through the zero-scope chat/triage routing path.
    # Merge context for routing decision
    routing_context = thread.context.copy()
    if context:
        routing_context.update(context)
    routing_context["task_id"] = task_id
    routing_context["thread_id"] = thread_id
    routing_context["session_id"] = thread.metadata.session_id or thread_id
    attached_target_handles = get_attached_target_handles_from_context(routing_context)

    # Ensure active_instance_id is in routing context
    if active_instance_id:
        routing_context["instance_id"] = active_instance_id
        routing_context.pop("cluster_id", None)
    elif active_cluster_id:
        routing_context["cluster_id"] = active_cluster_id
        routing_context.pop("instance_id", None)

    current_scope = materialize_turn_scope(routing_context, thread_id=thread_id, thread=thread)
    if staged_thread_instance is None:
        current_scope = await _ensure_handle_backed_turn_scope(
            turn_scope=current_scope,
            routing_context=routing_context,
            thread_context=thread.context,
            thread_id=thread_id,
            task_id=task_id,
            legacy_instance_id=(
                routing_context.get("instance_id") or current_scope.seed_hints.get("instance_id")
            ),
            legacy_cluster_id=(
                routing_context.get("cluster_id") or current_scope.seed_hints.get("cluster_id")
            ),
        )
    routing_context["turn_scope"] = current_scope.model_dump(mode="json")

    return PreparedTarget(
        context=routing_context,
        scope=current_scope,
        instance_id=active_instance_id,
        cluster_id=active_cluster_id,
        staged_instance=staged_thread_instance,
        explicit_scope=bool(instance_id_from_client or cluster_id_from_client),
        attached_handles=attached_target_handles,
    )


async def authorize_named_triage_targets(
    *, agent_type, message, thread, task_manager, thread_manager, task_id, thread_id
):
    """Check targets named in this turn even when the thread already has a binding."""
    if agent_type == AgentType.REDIS_TRIAGE and settings.infrastructure_authorization_enabled:
        from redis_sre_agent.core.authorization import (
            TargetRef as _TargetRef,
        )
        from redis_sre_agent.core.authorization import (
            assert_target_allowed as _assert_target_allowed,
        )
        from redis_sre_agent.core.targets import (
            resolve_target_query as _resolve_named_targets,
        )

        # ONLY the discovery step is wrapped: if resolution fails we can't identify a named
        # target, so there's nothing to deny and the base loaders still enforce access. The
        # authorization DECISION below runs OUTSIDE the try on purpose — assert_target_allowed
        # fail-closes internally (scope_targets returns [] on hook error/timeout, never raises),
        # so a denied named target is never silently swallowed by a broad catch-all.
        _named = None
        try:
            _named = await _resolve_named_targets(
                query=message,
                user_id=thread.metadata.user_id,
                allow_multiple=True,
                max_results=5,
                preferred_capabilities=["diagnostics", "admin", "cloud"],
                apply_scope=False,
            )
        except Exception:
            logger.exception(
                "named-target resolution failed for triage turn; base loaders still enforce access"
            )

        _denied_ids: list[str] = []
        _denied_names: list[str] = []
        _allowed_named = 0
        for _m in getattr(_named, "matches", None) or []:
            _rid = getattr(_m, "resource_id", None)
            if not _rid:
                continue  # unresolved reference -> no target to authorize
            if await _assert_target_allowed(
                _TargetRef(str(getattr(_m, "target_kind", "") or ""), str(_rid))
            ):
                _allowed_named += 1
            else:
                _denied_ids.append(str(_rid))
                _denied_names.append(str(getattr(_m, "display_name", None) or _rid))
        if _denied_ids:
            _names = ", ".join(_denied_names)
            if not _allowed_named:
                # Every named target is denied -> hard-deny. Return BEFORE any "continuing"
                # update (_complete_turn_authorization_denied logs its own) so the audit trail
                # isn't contradicted by a "continuing with the targets you can access" line.
                result = await _complete_turn_authorization_denied(
                    task_manager=task_manager,
                    thread_manager=thread_manager,
                    thread_id=thread_id,
                    task_id=task_id,
                    user_message=message,
                    response_text=f"You are not authorized to access the requested target(s): {_names}.",
                )
                return result
            # Some denied, some allowed -> note the denied set and continue with the allowed one.
            await task_manager.add_task_update(
                task_id,
                f"Not authorized for: {_names}. Continuing with the targets you can access.",
                "authorization_scoped",
                metadata={"unauthorized_targets": _denied_ids},
            )

    return None


async def discover_triage_targets(
    *, target, agent_type, message, thread, task_manager, thread_manager, task_id, thread_id
):
    """Resolve a zero-scope triage request or finish an over-limit discovery."""
    routing_context = target.context
    current_scope = target.scope
    active_instance_id = target.instance_id
    active_cluster_id = target.cluster_id
    attached_target_handles = target.attached_handles
    if (
        agent_type == AgentType.REDIS_TRIAGE
        and current_scope.scope_kind == "zero_scope"
        and not (active_instance_id or active_cluster_id or attached_target_handles)
    ):
        try:
            from redis_sre_agent.core.targets import (
                materialize_bound_target_scope,
                resolve_target_query,
            )

            resolution = await resolve_target_query(
                query=message,
                user_id=thread.metadata.user_id,
                allow_multiple=True,
                max_results=5,
                preferred_capabilities=["diagnostics", "admin", "cloud"],
            )
            if resolution.status == DISCOVERY_STATUS_TOO_MANY_MATCHES:
                result = await _complete_deep_triage_target_limit_response(
                    task_manager=task_manager,
                    thread_manager=thread_manager,
                    thread_id=thread_id,
                    task_id=task_id,
                    user_message=message,
                    resolution=resolution,
                )
                return result
            if (
                resolution.status == "resolved"
                and not resolution.clarification_required
                and resolution.selected_matches
            ):
                bound_scope = await materialize_bound_target_scope(
                    matches=resolution.selected_matches,
                    thread_id=thread_id,
                    task_id=task_id,
                    replace_existing=False,
                )
                if bound_scope.attached_bindings:
                    routing_context.update(bound_scope.context_updates)
                    await task_manager.add_task_update(
                        task_id,
                        "Resolved target scope from natural language before deep triage",
                        "target_resolution",
                        metadata={
                            "attached_target_handles": routing_context["attached_target_handles"],
                            "match_count": len(bound_scope.selected_bindings),
                        },
                    )
                    current_scope = materialize_turn_scope(
                        routing_context, thread_id=thread_id, thread=thread
                    )
        except Exception:
            logger.exception("Failed to pre-resolve target scope for deep triage")

    target.scope = current_scope
    return None


async def _resolve_instance_for_thread(
    instance_id: Optional[str], thread_id: Optional[str]
) -> Optional[RedisInstance]:
    """Resolve an instance ID against persistent storage, then thread-scoped session instances."""
    if not instance_id:
        return None

    instance = await get_instance_by_id(instance_id)
    if instance or not thread_id:
        return instance

    for session_instance in await get_session_instances(thread_id):
        if session_instance.id == instance_id:
            return session_instance
    return None


async def _stage_session_instance_from_message(
    *,
    thread_id: str,
    thread_user_id: Optional[str],
    instance_details: Dict[str, str],
) -> RedisInstance:
    """Stage extracted connection details as a thread-scoped instance without mutating global config."""
    connection_url = instance_details["connection_url"]
    name = instance_details["name"]

    for session_instance in await get_session_instances(thread_id):
        if (
            session_instance.name == name
            or session_instance.connection_url.get_secret_value() == connection_url
        ):
            return session_instance

    session_instance = RedisInstance(
        id=f"redis-{instance_details['environment']}-{ULID()}",
        name=name,
        connection_url=connection_url,
        environment=instance_details["environment"],
        usage=instance_details["usage"],
        description=instance_details.get(
            "description", "Staged by agent from user-provided connection details"
        ),
        created_by="agent",
        user_id=thread_user_id,
        instance_type=RedisInstanceType.unknown,
    )
    from .redis_topology import classify_redis_endpoint

    session_instance.instance_type = await classify_redis_endpoint(connection_url)
    if not await add_session_instance(thread_id, session_instance):
        raise ValueError("Failed to stage session instance from provided details")
    return session_instance


async def _ensure_handle_backed_turn_scope(
    *,
    turn_scope: TurnScope,
    routing_context: Dict[str, Any],
    thread_context: Dict[str, Any],
    thread_id: str,
    task_id: str,
    legacy_instance_id: Optional[str] = None,
    legacy_cluster_id: Optional[str] = None,
) -> TurnScope:
    """Materialize legacy seed-hint scope through private handle records when needed."""
    from redis_sre_agent.core.targets import (
        build_seed_hint_candidates,
        materialize_bound_target_scope,
    )
    from redis_sre_agent.targets import get_target_handle_store

    normalized_instance_id = str(legacy_instance_id or "").strip() or None
    normalized_cluster_id = str(legacy_cluster_id or "").strip() or None

    if normalized_instance_id and normalized_cluster_id:
        routing_instance_id = str(routing_context.get("instance_id") or "").strip() or None
        routing_cluster_id = str(routing_context.get("cluster_id") or "").strip() or None

        if turn_scope.bindings:
            # Attached bindings already define the target set. Ignore conflicting
            # legacy single-target hints so the seed-hint resolver does not raise.
            normalized_instance_id = None
            normalized_cluster_id = None
        elif routing_instance_id and not routing_cluster_id:
            normalized_cluster_id = None
        elif routing_cluster_id and not routing_instance_id:
            normalized_instance_id = None
        else:
            # Conflicting legacy single-target hints are ambiguous. Drop both
            # rather than raising while rebuilding handle-backed scope.
            normalized_instance_id = None
            normalized_cluster_id = None

    needs_materialization = False
    if turn_scope.scope_kind != "target_bindings":
        needs_materialization = bool(normalized_instance_id or normalized_cluster_id)
    elif turn_scope.bindings:
        bound_handles = [binding.target_handle for binding in turn_scope.bindings]
        handle_records = await get_target_handle_store().get_records(bound_handles)
        needs_materialization = any(
            binding.target_handle not in handle_records for binding in turn_scope.bindings
        )

    if not needs_materialization:
        return turn_scope

    candidates = await build_seed_hint_candidates(
        bindings=turn_scope.bindings,
        instance_id=normalized_instance_id,
        cluster_id=normalized_cluster_id,
    )
    if not candidates:
        return turn_scope

    materialized_scope = await materialize_bound_target_scope(
        matches=candidates,
        thread_id=thread_id,
        task_id=task_id,
        replace_existing=bool(turn_scope.bindings),
    )
    scope_updates = dict(materialized_scope.context_updates)
    if normalized_instance_id:
        scope_updates["instance_id"] = normalized_instance_id
        scope_updates["cluster_id"] = ""
    elif normalized_cluster_id:
        scope_updates["cluster_id"] = normalized_cluster_id
        scope_updates["instance_id"] = ""

    routing_context.update(scope_updates)
    thread_context.update(scope_updates)

    return TurnScope.from_context(
        routing_context,
        thread_id=thread_id,
        session_id=turn_scope.session_id,
        seed_hints={
            key: routing_context[key]
            for key in ("instance_id", "cluster_id")
            if routing_context.get(key)
        },
    )
