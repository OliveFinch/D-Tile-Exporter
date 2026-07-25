"""Display helpers, sharing the library's formatting so numbers agree."""

from __future__ import annotations

from tilearc.util import human_bytes, human_duration

__all__ = ["human_bytes", "human_duration", "severity_colour"]

_SEVERITY_COLOURS = {
    "error": "#c0392b",
    "warning": "#b36b00",
    "info": "#5a5a5a",
}


def severity_colour(severity: str) -> str:
    return _SEVERITY_COLOURS.get(severity, "#5a5a5a")
