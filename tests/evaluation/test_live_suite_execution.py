from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import redis_sre_agent.evaluation.live_suite as live_suite_module
from redis_sre_agent.evaluation.live_suite import LiveEvalScenarioResult, run_live_eval_suite
from redis_sre_agent.evaluation.report_schema import EvalReportBundle


def _write_scenario(path: Path, scenario_id: str) -> None:
    path.write_text(
        f"""
id: {scenario_id}
name: {scenario_id}
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
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_live_suite_runs_only_selected_scenarios_in_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    _write_scenario(first, "prompt/first")
    _write_scenario(second, "prompt/second")
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        """
suites:
  example:
    scenarios:
      - first.yaml
      - second.yaml
""",
        encoding="utf-8",
    )
    called: list[str] = []

    @asynccontextmanager
    async def fake_redis_client():
        yield object()

    async def fake_run_scenario(scenario, *, output_dir: Path, git_sha: str, **kwargs):
        called.append(scenario.id)
        report_dir = output_dir / scenario.id
        report_dir.mkdir(parents=True)
        report_path = report_dir / "report.json"
        report_path.write_text(
            EvalReportBundle(
                scenario_id=scenario.id,
                git_sha=git_sha,
                execution_lane="agent_only",
                overall_pass=True,
            ).model_dump_json(),
            encoding="utf-8",
        )
        markdown_path = report_dir / "report.md"
        markdown_path.write_text("ok", encoding="utf-8")
        return LiveEvalScenarioResult(
            scenario_id=scenario.id,
            execution_lane="agent_only",
            overall_pass=True,
            report_json=str(report_path),
            report_markdown=str(markdown_path),
        )

    monkeypatch.setattr(live_suite_module, "_live_eval_redis_client", fake_redis_client)
    monkeypatch.setattr(live_suite_module, "_run_scenario_live", fake_run_scenario)
    monkeypatch.setattr(live_suite_module, "_current_git_sha", lambda: "abc123")

    summary = await run_live_eval_suite(
        "example",
        config_path=suite_path,
        output_dir=tmp_path / "reports",
        scenario_ids=["prompt/second"],
    )

    assert called == ["prompt/second"]
    assert summary.total_scenarios == 1


@pytest.mark.asyncio
async def test_live_suite_rejects_unknown_scenario_ids(
    tmp_path: Path,
) -> None:
    scenario_path = tmp_path / "scenario.yaml"
    _write_scenario(scenario_path, "prompt/example")
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        """
suites:
  example:
    scenarios:
      - scenario.yaml
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown scenario ids"):
        await run_live_eval_suite(
            "example",
            config_path=suite_path,
            output_dir=tmp_path / "reports",
            scenario_ids=["prompt/missing"],
        )
