"""Sanity checks on hand-maintained ``boundsByZoom`` data.

``boundsByZoom`` is edited by hand and has drifted. This module reports what
looks wrong and leaves the fixing to a human -- it never rewrites a config and
the downloader never "corrects" bounds on the fly. A silently widened rectangle
would quietly add thousands of 404s to a job; a silently narrowed one would
quietly omit part of the map from the archive.

Rules
-----
``span``      each zoom should be at least twice as wide/tall as its parent,
              since one tile subdivides into four.
``coverage``  the child rectangle should cover exactly the parent's area:
              ``min*2 .. max*2+1``. This is the sharp version of the span rule
              and pinpoints off-by-one data entry.
``aspect``    the width:height ratio should not invert between neighbours --
              that is the signature of X and Y being swapped on entry.
``zoom-range`` bounds keys outside ``[minZoom, maxZoom]`` are dead weight, and
              zooms inside the range with no entry cannot be downloaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .config import ParkConfig, TileBounds

SEVERITIES = ("error", "warning", "info")

#: How far from square a rectangle must be before an inversion means anything.
#: A 10x9 level "inverting" to 8x10 is just a near-square wobbling across 1.0,
#: not evidence of swapped axes -- and reporting it buries the real cases.
ASPECT_SIGNIFICANCE = 1.3


@dataclass(frozen=True)
class Finding:
    park: str
    zoom: int | None
    rule: str
    severity: str
    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "park": self.park,
            "zoom": self.zoom,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


def _aspect(bounds: TileBounds) -> float:
    return bounds.width / bounds.height


def _is_significant_inversion(parent_aspect: float, child_aspect: float) -> bool:
    if not ((parent_aspect > 1.0) ^ (child_aspect > 1.0)):
        return False
    parent_skew = max(parent_aspect, 1 / parent_aspect)
    child_skew = max(child_aspect, 1 / child_aspect)
    return parent_skew >= ASPECT_SIGNIFICANCE and child_skew >= ASPECT_SIGNIFICANCE


def check_park(config: ParkConfig) -> list[Finding]:
    findings: list[Finding] = []
    park = config.park_id
    zooms = config.bounds_zooms

    # -- zoom range vs bounds keys ----------------------------------------
    for zoom in zooms:
        if zoom < config.min_zoom or zoom > config.max_zoom:
            findings.append(
                Finding(
                    park,
                    zoom,
                    "zoom-range",
                    "info",
                    f"z{zoom} has bounds but sits outside the park's zoom range "
                    f"({config.min_zoom}-{config.max_zoom}); it will never be downloaded",
                    {"minZoom": config.min_zoom, "maxZoom": config.max_zoom},
                )
            )
    for zoom in range(config.min_zoom, config.max_zoom + 1):
        if zoom not in config.bounds_by_zoom:
            findings.append(
                Finding(
                    park,
                    zoom,
                    "zoom-range",
                    "warning",
                    f"z{zoom} is inside the park's zoom range "
                    f"({config.min_zoom}-{config.max_zoom}) but has no boundsByZoom entry, "
                    f"so it will be skipped",
                    {"minZoom": config.min_zoom, "maxZoom": config.max_zoom},
                )
            )

    # -- parent/child comparisons -----------------------------------------
    for parent_zoom, child_zoom in zip(zooms, zooms[1:]):
        if child_zoom != parent_zoom + 1:
            continue  # not adjacent; nothing meaningful to compare
        parent = config.bounds_by_zoom[parent_zoom]
        child = config.bounds_by_zoom[child_zoom]

        for axis, p_span, c_span in (
            ("x", parent.width, child.width),
            ("y", parent.height, child.height),
        ):
            if c_span < p_span * 2:
                findings.append(
                    Finding(
                        park,
                        child_zoom,
                        "span",
                        "error",
                        f"z{child_zoom} {axis}-span is {c_span}, less than 2x z{parent_zoom}'s "
                        f"{p_span} (expected >= {p_span * 2})",
                        {
                            "axis": axis,
                            "parentZoom": parent_zoom,
                            "parentSpan": p_span,
                            "childSpan": c_span,
                            "expectedMin": p_span * 2,
                        },
                    )
                )

        expected = TileBounds(
            parent.min_x * 2, parent.max_x * 2 + 1, parent.min_y * 2, parent.max_y * 2 + 1
        )
        edges = {
            "minX": (child.min_x, expected.min_x),
            "maxX": (child.max_x, expected.max_x),
            "minY": (child.min_y, expected.min_y),
            "maxY": (child.max_y, expected.max_y),
        }
        # A child that starts later or ends earlier than the parent's footprint
        # is losing coverage the parent claims exists.
        shortfalls = {
            name: (actual, want)
            for name, (actual, want) in edges.items()
            if (name.startswith("min") and actual > want)
            or (name.startswith("max") and actual < want)
        }
        if shortfalls:
            parts = ", ".join(
                f"{name} {actual} (parent implies {want})"
                for name, (actual, want) in sorted(shortfalls.items())
            )
            findings.append(
                Finding(
                    park,
                    child_zoom,
                    "coverage",
                    "warning",
                    f"z{child_zoom} does not cover all of z{parent_zoom}: {parts}",
                    {
                        "parentZoom": parent_zoom,
                        "expected": expected.as_dict(),
                        "actual": child.as_dict(),
                        "shortfalls": {k: v[0] for k, v in shortfalls.items()},
                    },
                )
            )

        p_aspect, c_aspect = _aspect(parent), _aspect(child)
        if _is_significant_inversion(p_aspect, c_aspect):
            findings.append(
                Finding(
                    park,
                    child_zoom,
                    "aspect",
                    "error",
                    f"z{child_zoom} aspect ratio inverts vs z{parent_zoom} "
                    f"({parent.width}x{parent.height} -> {child.width}x{child.height}); "
                    f"X and Y may have been swapped on entry",
                    {
                        "parentZoom": parent_zoom,
                        "parent": f"{parent.width}x{parent.height}",
                        "child": f"{child.width}x{child.height}",
                        "parentAspect": round(p_aspect, 3),
                        "childAspect": round(c_aspect, 3),
                    },
                )
            )

    return findings


def check_all(configs: Iterable[ParkConfig]) -> list[Finding]:
    findings: list[Finding] = []
    for config in configs:
        findings.extend(check_park(config))
    return findings


def worst_severity(findings: Iterable[Finding]) -> str | None:
    seen = {f.severity for f in findings}
    for severity in SEVERITIES:
        if severity in seen:
            return severity
    return None
