from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from redis_sre_agent.api.evals import EvalControlPlaneServices, router
from redis_sre_agent.evaluation.control_plane.comparison import EvalComparisonService
from redis_sre_agent.evaluation.control_plane.models import EvalRunRecord
from redis_sre_agent.evaluation.control_plane.store import FileEvalRunStore
from tests.evaluation.control_plane.test_worker import _registry


class FakeManager:
    def __init__(self, registry, store) -> None:
        self.registry = registry
        self.store = store

    async def create_run(self, suite_id: str, *, requested_by: str = "local") -> EvalRunRecord:
        suite = self.registry.get(suite_id)
        record = EvalRunRecord(
            run_id="01J00000000000000000000001",
            suite_id=suite.id,
            suite_name=suite.name,
            suite_manifest=suite.manifest,
            suite_digest=suite.digest,
            scenario_ids=[scenario.id for scenario in suite.scenarios],
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


def test_create_run_accepts_only_suite_id(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/api/v1/evals/runs", json={"suite_id": "example"})
    invalid = client.post(
        "/api/v1/evals/runs",
        json={"suite_id": "example", "config_path": "../../arbitrary.yaml"},
    )

    assert response.status_code == 202
    assert response.json()["suite_id"] == "example"
    assert invalid.status_code == 422


def test_disabled_control_plane_returns_503(tmp_path: Path) -> None:
    response = _client(tmp_path, enabled=False).get("/api/v1/evals/suites")

    assert response.status_code == 503
    assert "EVAL_CONTROL_ENABLED=true" in response.json()["detail"]
