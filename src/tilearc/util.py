"""Small helpers: path sanitising, human-readable numbers, time formatting."""

from __future__ import annotations

import hashlib
import re

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Reserved device names on Windows; a bare "con.jpg" is unopenable there.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(value: object, fallback: str = "unknown", max_len: int = 64) -> str:
    """Turn an opaque identifier into a safe single path component.

    Version codes are arbitrary upstream strings -- ``"47"``, ``"801755166"``,
    ``"jan2026"``, a 14-digit TDR timestamp -- and nothing guarantees they stay
    filesystem-safe. Anything outside ``[A-Za-z0-9._-]`` becomes ``_``.

    Because that mapping is lossy (``a/b`` and ``a_b`` would collide), a short
    digest of the original is appended whenever the value actually changed, so
    distinct inputs always produce distinct outputs.
    """
    original = str(value)
    cleaned = _UNSAFE.sub("_", original.strip()).strip("._")
    if not cleaned:
        cleaned = fallback
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("._") or fallback
    if cleaned != original:
        digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:6]
        cleaned = f"{cleaned}-{digest}"
    return cleaned


def human_bytes(count: float) -> str:
    """Format a byte count using SI (decimal) units, matching disk-vendor maths."""
    step = 1000.0
    units = ("B", "kB", "MB", "GB", "TB", "PB")
    value = float(count)
    for unit in units:
        if abs(value) < step or unit == units[-1]:
            if unit == "B":
                return f"{value:.0f} B"
            return f"{value:.1f} {unit}"
        value /= step
    raise AssertionError("unreachable")


def human_count(count: int) -> str:
    return f"{count:,}"


def human_duration(seconds: float) -> str:
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        return "--:--"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
