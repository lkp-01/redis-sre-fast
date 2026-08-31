"""Discovery and validation of trusted eval suite manifests."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from redis_sre_agent.evaluation.live_suite import load_live_eval_suite_config
from redis_sre_agent.evaluation.runtime import load_eval_scenario

from .models import (
    SuiteDescriptor,
    SuiteDiscoveryError,
    SuiteDiscoveryResult,
    SuiteScenarioDescriptor,
)


class SuiteRegistry:
    """Discover suites from one configured root; callers never supply manifest paths."""

    def __init__(self, suite_root: str | Path) -> None:
        self.root = Path(suite_root).expanduser().resolve()

    @staticmethod
    def _resolve_reference(manifest: Path, reference: str) -> Path:
        candidate = Path(reference).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        owner_relative = manifest.parent / candidate
        if owner_relative.exists():
            return owner_relative.resolve()
        if candidate.exists():
            return candidate.resolve()
        return owner_relative.resolve()

    def _relative_manifest(self, manifest: Path) -> str:
        return manifest.relative_to(self.root).as_posix()

    def _validate_fixture_path(self, path: Path) -> Path:
        eval_root = self.root.parent.resolve()
        resolved = path.resolve()
        if resolved != eval_root and eval_root not in resolved.parents:
            raise ValueError(f"suite reference escapes eval fixture root: {path}")
        return resolved

    def _build_descriptor(self, manifest: Path, suite_id: str, suite: object) -> SuiteDescriptor:
        scenarios: list[SuiteScenarioDescriptor] = []
        digest = hashlib.sha256()
        digest.update(manifest.read_bytes())
        if suite.policy_file:
            policy_path = self._validate_fixture_path(
                self._resolve_reference(manifest, str(suite.policy_file))
            )
            digest.update(b"\0policy\0")
            digest.update(policy_path.read_bytes())
        for reference in suite.scenarios:
            scenario_path = self._validate_fixture_path(
                self._resolve_reference(manifest, str(reference))
            )
            scenario = load_eval_scenario(scenario_path)
            digest.update(b"\0")
            digest.update(scenario_path.read_bytes())
            scenarios.append(
                SuiteScenarioDescriptor(
                    id=scenario.id,
                    name=scenario.name,
                    description=scenario.description,
                    lane=scenario.execution.lane,
                )
            )
        return SuiteDescriptor(
            id=suite_id,
            name=suite.name,
            description=suite.description,
            manifest=self._relative_manifest(manifest),
            config_path=str(manifest),
            digest=digest.hexdigest(),
            scenario_count=len(scenarios),
            scenarios=scenarios,
            judge_pass_threshold=suite.judge_pass_threshold,
            baseline_profile=suite.baseline_profile,
            baseline_policy=suite.baseline_policy,
        )

    def discover(self) -> SuiteDiscoveryResult:
        if not self.root.is_dir():
            return SuiteDiscoveryResult(
                errors=[
                    SuiteDiscoveryError(
                        manifest=".", error=f"suite root does not exist: {self.root}"
                    )
                ]
            )

        candidates: list[SuiteDescriptor] = []
        errors: list[SuiteDiscoveryError] = []
        manifests = sorted({*self.root.rglob("*.yaml"), *self.root.rglob("*.yml")})
        for manifest in manifests:
            relative = self._relative_manifest(manifest)
            try:
                config = load_live_eval_suite_config(manifest)
                for suite_id, suite in config.suites.items():
                    candidates.append(self._build_descriptor(manifest, suite_id, suite))
            except Exception as exc:
                errors.append(SuiteDiscoveryError(manifest=relative, error=str(exc)))

        grouped: dict[str, list[SuiteDescriptor]] = defaultdict(list)
        for descriptor in candidates:
            grouped[descriptor.id].append(descriptor)

        suites: list[SuiteDescriptor] = []
        for suite_id, descriptors in grouped.items():
            if len(descriptors) == 1:
                suites.append(descriptors[0])
                continue
            for descriptor in descriptors:
                errors.append(
                    SuiteDiscoveryError(
                        manifest=descriptor.manifest,
                        error=f"duplicate suite id: {suite_id}",
                    )
                )
        suites.sort(key=lambda item: item.id)
        errors.sort(key=lambda item: (item.manifest, item.error))
        return SuiteDiscoveryResult(suites=suites, errors=errors)

    def get(self, suite_id: str) -> SuiteDescriptor:
        result = self.discover()
        for suite in result.suites:
            if suite.id == suite_id:
                return suite
        raise KeyError(suite_id)


__all__ = ["SuiteRegistry"]
