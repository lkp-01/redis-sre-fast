import pytest
from pydantic import ValidationError

from redis_sre_agent.core.turn_scope import TurnScope


def test_turn_scope_discards_removed_support_package_context() -> None:
    scope = TurnScope.from_context(
        {"support_package_id": "legacy-package", "support_package_path": "/tmp/legacy"}
    )

    assert scope.scope_kind == "zero_scope"
    assert "support_package_id" not in scope.to_thread_context()
    assert "support_package_path" not in scope.to_thread_context()


def test_turn_scope_rejects_removed_support_package_kind() -> None:
    with pytest.raises(ValidationError):
        TurnScope(scope_kind="support_package")
