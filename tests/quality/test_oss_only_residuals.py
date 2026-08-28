"""Regression test for the repository-level OSS-only residual checker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_oss_only_residual_checker_passes_for_workspace() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/quality/check_oss_only_residuals.py"), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
