from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from redis_sre_agent.api.evals import EvalControlPlaneServices, router
from redis_sre_agent.evaluation.control_plane.comparison import EvalComparisonService
from redis_sre_agent.evaluation.control_plane.manager import InvalidScenarioSelectionError
from redis_sre_agent.evaluation.control_plane.models import EvalRunRecord
from redis_sre_agent.evaluation.control_plane.store import FileEvalRunStore
from tests.evaluation.control_plane.test_worker import _registry


class FakeManager:
    def __init__(self, registry, store) -> None:
        self.registry = registry
        self.store = store

    async def create_run(
        self,
        suite_id: str,
        *,
        requested_by: str = "local",
        scenario_ids: list[str] | None = None,
    ) -> EvalRunRecord:
        suite = self.registry.get(suite_id)
        if scenario_ids == ["prompt/missing"]:
            raise InvalidScenarioSelectionError(
                suite_id=suite_id,
                unknown_ids=scenario_ids,
            )
        selected_ids = scenario_ids or [scenario.id for scenario in suite.scenarios]
        record = EvalRunRecord(
            run_id="01J00000000000000000000001",
            suite_id=suite.id,
            suite_name=suite.name,
            suite_manifest=suite.manifest,
            suite_digest=suite.digest,
            scenario_ids=selected_ids,
            is_partial=len(selected_ids) < len(suite.scenarios),
            requested_by=requested_by,
            git_sha="abc123",
        )
        self.store.save(record)
        return record


def _client(tmp_path: Path, *, enabled: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if enabled:
        registry = _registry(tmp_path)
        store = FileEvalRunStore(tmp_path / "control")
        app.state.eval_control_plane = EvalControlPlaneServices(
            registry=registry,
            store=store,
            manager=FakeManager(registry, store),  # type: ignore[arg-type]
            comparison=EvalComparisonService(store),
        )
    return TestClient(app)


def test_suite_api_is_registry_driven_and_hides_internal_config_path(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/v1/evals/suites")

    assert response.status_code == 200
    suite = response.json()["suites"][0]
    assert suite["id"] == "example"
    assert suite["scenario_count"] == 1
    assert "config_path" not in suite


def test_create_run_accepts_suite_id_and_optional_scenario_ids(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/api/v1/evals/runs", json={"suite_id": "example"})
    selected = client.post(
        "/api/v1/evals/runs",
        json={"suite_id": "example", "scenario_ids": ["prompt/example"]},
    )
    invalid = client.post(
        "/api/v1/evals/runs",
        json={"suite_id": "example", "config_path": "../../arbitrary.yaml"},
    )

    assert response.status_code == 202
    assert response.json()["suite_id"] == "example"
    assert selected.status_code == 202
    assert selected.json()["scenario_ids"] == ["prompt/example"]
    assert invalid.status_code == 422


def test_create_run_rejects_empty_or_unknown_scenario_selection(tmp_path: Path) -> None:
    client = _client(tmp_path)

    empty = client.post(
        "/api/v1/evals/runs",
        json={"suite_id": "example", "scenario_ids": []},
    )
    unknown = client.post(
        "/api/v1/evals/runs",
        json={"suite_id": "example", "scenario_ids": ["prompt/missing"]},
    )

    assert empty.status_code == 422
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["unknown_scenario_ids"] == ["prompt/missing"]


def test_disabled_control_plane_returns_503(tmp_path: Path) -> None:
    response = _client(tmp_path, enabled=False).get("/api/v1/evals/suites")

    assert response.status_code == 503
    assert "EVAL_CONTROL_ENABLED=true" in response.json()["detail"]
