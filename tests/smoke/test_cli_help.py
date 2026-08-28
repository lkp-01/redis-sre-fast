"""Smoke tests for the installed CLI entry point."""

from click.testing import CliRunner

from redis_sre_agent.cli.main import main


def test_cli_help_excludes_removed_package_commands() -> None:
    """The public CLI exposes only the protocol-oriented command set."""
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0, result.output
    assert "support-package" not in result.output
