"""Fail when removed provider terminology appears outside intentional compatibility tests."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from zipfile import ZipFile

DEFAULT_TARGETS = (
    "redis_sre_agent",
    "evals",
    "monitoring",
    "pyproject.toml",
    "uv.lock",
    "scripts/migrations",
    "tests",
)
MARKERS = (
    "redis_cloud",
    "redis cloud",
    "redis_enterprise",
    "redis enterprise",
    "support_package",
    "support package",
    "enterprise_admin",
    "rladmin",
    "admin_url",
    "admin_username",
    "admin_password",
    "redislabs.com",
)
MARKER_PATTERN = re.compile("|".join(re.escape(marker) for marker in MARKERS), re.IGNORECASE)
ABBREVIATION_PATTERN = re.compile(r"(?<![\\w\\\\])(?:crdb|bdb)(?!\\w)", re.IGNORECASE)


def _read_allowlist(path: Path) -> tuple[set[str], set[tuple[str, int]]]:
    allowed_paths: set[str] = set()
    allowed_lines: set[tuple[str, int]] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        entry = raw_line.strip().replace("\\", "/")
        if not entry or entry.startswith("#"):
            continue
        path_part, separator, line_part = entry.rpartition(":")
        if separator and line_part.isdigit():
            allowed_lines.add((path_part, int(line_part)))
        else:
            allowed_paths.add(entry)
    return allowed_paths, allowed_lines


def _is_allowed(
    relative_path: str,
    line_number: int,
    allowed_paths: set[str],
    allowed_lines: set[tuple[str, int]],
) -> bool:
    if (relative_path, line_number) in allowed_lines:
        return True
    return any(
        relative_path == allowed_path or relative_path.startswith(f"{allowed_path.rstrip('/')}/")
        for allowed_path in allowed_paths
    )


def _iter_workspace_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for target in DEFAULT_TARGETS:
        candidate = root / target
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
            files.extend(path for path in candidate.rglob("*") if path.is_file())
    return sorted(files)


def _find_text_violations(
    relative_path: str,
    text: str,
    allowed_paths: set[str],
    allowed_lines: set[tuple[str, int]],
) -> list[str]:
    violations: list[str] = []
    path_match = MARKER_PATTERN.search(relative_path)
    if path_match and not _is_allowed(relative_path, 0, allowed_paths, allowed_lines):
        violations.append(f"{relative_path}:path:{path_match.group(0)}")
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = MARKER_PATTERN.search(line)
        if match is None and relative_path != "uv.lock":
            match = ABBREVIATION_PATTERN.search(line)
        if match and not _is_allowed(relative_path, line_number, allowed_paths, allowed_lines):
            violations.append(f"{relative_path}:{line_number}:{match.group(0)}")
    return violations


def check_workspace(root: Path, allowlist_path: Path) -> list[str]:
    allowed_paths, allowed_lines = _read_allowlist(allowlist_path)
    violations: list[str] = []
    for path in _iter_workspace_files(root):
        relative_path = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        violations.extend(
            _find_text_violations(relative_path, text, allowed_paths, allowed_lines)
        )
    return violations


def check_wheel(wheel_path: Path) -> list[str]:
    violations: list[str] = []
    text_suffixes = {".py", ".md", ".json", ".toml", ".txt", ".yaml", ".yml"}
    with ZipFile(wheel_path) as archive:
        for name in archive.namelist():
            path_match = MARKER_PATTERN.search(name)
            if path_match:
                violations.append(f"{wheel_path.name}!{name}:path:{path_match.group(0)}")
            if Path(name).suffix.lower() not in text_suffixes and "METADATA" not in name:
                continue
            try:
                text = archive.read(name).decode("utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if (
                    name
                    in {
                        "redis_sre_agent/agent/chat_agent.py",
                        "redis_sre_agent/agent/prompts.py",
                    }
                    and "offline support packages" in line
                ):
                    continue
                match = MARKER_PATTERN.search(line)
                if match:
                    violations.append(f"{wheel_path.name}!{name}:{line_number}:{match.group(0)}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path(__file__).with_name("oss_only_residual_allowlist.txt"),
    )
    parser.add_argument("--wheel", type=Path, action="append", default=[])
    args = parser.parse_args()

    root = args.root.resolve()
    violations = check_workspace(root, args.allowlist.resolve())
    for wheel_path in args.wheel:
        violations.extend(check_wheel(wheel_path.resolve()))
    if violations:
        print("OSS-only residual check failed:", file=sys.stderr)
        print("\n".join(f"  {violation}" for violation in violations), file=sys.stderr)
        return 1
    print("OSS-only residual check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
