"""Cross-platform CLI output helpers."""

from __future__ import annotations

import sys
from typing import TextIO


def configure_cli_output(stream: TextIO | None = None) -> None:
    """Prevent unsupported terminal glyphs from aborting CLI commands.

    Keep the terminal's selected encoding so localized text remains readable,
    but replace individual characters that the encoding cannot represent.
    """
    active_stream = stream or sys.stdout
    reconfigure = getattr(active_stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(errors="replace")
    except (OSError, ValueError):
        # Capture streams and embedded hosts may reject reconfiguration.
        return
