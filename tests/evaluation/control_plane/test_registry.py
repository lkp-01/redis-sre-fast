from __future__ import annotations

from pathlib import Path

from redis_sre_agent.evaluation.control_plane.registry import SuiteRegistry

SCENARIO = """
id: prompt/example
name: Example scenario
description: Registry fixture
provenance:
  source_kind: synthetic
  source_pack: tests
  source_pack_version: "1"
  golden:
    expectation_basis: human_authored
    review_status: reviewed
execution:
  lane: agent_only
  agent: chat
  query: hello
"""


def test_registry_discovers_valid_suites_and_isolates_invalid_manifests(tmp_path: Path) -> None:
    suite_root = tmp_path / "suites"
    scenario_root = tmp_path / "scenarios"
    suite_root.mkdir()
    scenario_root.mkdir()
    (scenario_root / "scenario.yaml").write_text(SCENARIO, encoding="utf-8")
    (suite_root / "example.yaml").write_text(
        """
suites:
  example:
    description: Example suite
    judge_pass_threshold: 75
    scenarios:
      - ../scenarios/scenario.yaml
""",
        encoding="utf-8",
    )
    (suite_root / "broken.yaml").write_text("suites: [", encoding="utf-8")

    result = SuiteRegistry(suite_root).discover()

    assert [suite.id for suite in result.suites] == ["example"]
    assert result.suites[0].scenario_count == 1
    assert result.suites[0].scenarios[0].id == "prompt/example"
    assert result.suites[0].judge_pass_threshold == 75
    assert len(result.errors) == 1
    assert result.errors[0].manifest == "broken.yaml"


def test_registry_rejects_duplicate_suite_ids(tmp_path: Path) -> None:
    suite_root = tmp_path / "suites"
    scenario_root = tmp_path / "scenarios"
    suite_root.mkdir()
    scenario_root.mkdir()
    (scenario_root / "scenario.yaml").write_text(SCENARIO, encoding="utf-8")
    manifest = """
suites:
  duplicate:
    scenarios:
      - ../scenarios/scenario.yaml
"""
    (suite_root / "a.yaml").write_text(manifest, encoding="utf-8")
    (suite_root / "b.yaml").write_text(manifest, encoding="utf-8")

    result = SuiteRegistry(suite_root).discover()

    assert result.suites == []
    assert len(result.errors) == 2
    assert all("duplicate suite id" in error.error for error in result.errors)
