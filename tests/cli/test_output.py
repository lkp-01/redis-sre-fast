"""Tests for cross-platform CLI output configuration."""

import io

from redis_sre_agent.cli.output import configure_cli_output


def test_configure_cli_output_replaces_unencodable_characters() -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")

    configure_cli_output(stream)
    stream.write("中文📎")
    stream.flush()

    assert stream.errors == "replace"
    assert buffer.getvalue().decode("gbk") == "中文?"
