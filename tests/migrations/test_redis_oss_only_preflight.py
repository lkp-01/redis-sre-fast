import importlib.util
from pathlib import Path


def _load_script(name: str):
    path = Path("scripts/migrations") / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_preflight_redacts_secrets_and_reports_legacy_fields() -> None:
    module = _load_script("redis_oss_only_preflight.py")
    finding = module.inspect_record(
        "sre_instances:one",
        {"instance_type": "redis_cloud", "connection_url": "redis://user:secret@host", "admin_password": "secret"},
    )

    assert finding["type"] == "redis_cloud"
    assert finding["payload"]["connection_url"] == "***"
    assert finding["payload"]["admin_password"] == "***"


def test_apply_requires_backup_and_explicit_confirmation(tmp_path) -> None:
    module = _load_script("redis_oss_only_apply.py")
    report = tmp_path / "report.json"
    report.write_text('{"mode": "dry-run", "blocking_count": 0}', encoding="utf-8")

    try:
        module.validate_apply_request(report, tmp_path / "existing-backup.json", False)
    except ValueError as error:
        assert "confirm" in str(error)
    else:
        raise AssertionError("missing confirmation must block migration")
