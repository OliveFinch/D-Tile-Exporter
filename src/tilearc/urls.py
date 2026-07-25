"""Building tile URLs, and deciding what a given HTTP response means."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from . import USER_AGENT
from .config import ParkConfig, VersionEntry
from .errors import ConfigError

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class Outcome:
    """How a response was classified."""

    OK = "ok"
    MISSING = "missing"
    RETRY = "retry"
    AUTH = "auth"


@dataclass
class TileSource:
    """Everything needed to fetch tiles for one park/version/mode.

    ``missing_statuses`` differs per park and that difference matters:

    * CDN parks answer 403/404 for tiles outside the drawn area. Those are
      normal -- park bounds are rectangles, coverage isn't -- so they are
      recorded as missing and never retried.
    * The TDR worker collapses upstream 403/404 into **204 with an empty body**,
      so 204 and zero-length responses are the missing markers there. A direct
      403 from TDR means the signed cookies are bad, not that a tile is absent.
    """

    name: str
    template: str
    extension: str = "jpg"
    headers: Mapping[str, str] = field(default_factory=dict)
    cookies: Mapping[str, str] = field(default_factory=dict)
    missing_statuses: frozenset[int] = frozenset({403, 404})
    auth_statuses: frozenset[int] = frozenset()
    empty_is_missing: bool = True
    #: True when requests are billed against the user's shared Worker quota.
    uses_shared_proxy: bool = False
    #: Human-readable hint printed when everything comes back missing.
    all_missing_hint: str | None = None

    def url(self, zoom: int, x: int, y: int) -> str:
        return (
            self.template.replace("{z}", str(zoom))
            .replace("{x}", str(x))
            .replace("{y}", str(y))
        )

    def request_headers(self) -> dict[str, str]:
        """Headers to send, with any cookies rendered into a ``Cookie`` header.

        Sent as a plain header rather than through httpx's cookie jar: these are
        fixed signed values, not session state, and there is nothing to persist
        between requests.
        """
        headers = dict(self.headers)
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return headers

    def classify(self, status: int, body_len: int) -> str:
        if status in self.auth_statuses:
            return Outcome.AUTH
        if status in self.missing_statuses:
            return Outcome.MISSING
        if status == 200:
            if self.empty_is_missing and body_len == 0:
                return Outcome.MISSING
            return Outcome.OK
        if status == 429 or status >= 500 or status in (408, 425):
            return Outcome.RETRY
        # Anything else (301 loops, 400, 451...) is not worth hammering.
        return Outcome.RETRY if status < 400 else Outcome.MISSING


def _validate_template(template: str, *, allow_code: bool) -> None:
    found = set(_PLACEHOLDER.findall(template))
    required = {"z", "x", "y"}
    missing = required - found
    if missing:
        raise ConfigError(
            f"tile template {template!r} is missing placeholder(s): "
            + ", ".join("{" + m + "}" for m in sorted(missing))
        )
    unknown = found - required - ({"code"} if allow_code else set())
    if unknown:
        raise ConfigError(
            f"tile template {template!r} has unsupported placeholder(s): "
            + ", ".join("{" + u + "}" for u in sorted(unknown))
        )


def resolve_template(config: ParkConfig, version: VersionEntry) -> str:
    """Pick the template for this version, honouring a per-version override.

    Gotcha #1: an entry in ``{id}_dis_servers.json`` may carry its own ``url``,
    which wins over the park's ``tileTemplate``. DLP's ``jan2026`` does exactly
    this -- it points at an R2 bucket rather than Disney's CDN -- so ignoring
    the override silently archives the wrong imagery from the wrong host.
    """
    template = version.url or config.tile_template
    if not template:
        raise ConfigError(
            f"park '{config.park_id}' has no tileTemplate and version "
            f"'{version.code}' has no url override; nothing to fetch"
        )
    _validate_template(template, allow_code=True)
    return template


def build_source(config: ParkConfig, version: VersionEntry) -> TileSource:
    """Build a :class:`TileSource` for a public, template-driven park."""
    template = resolve_template(config, version)
    # DLP's park template carries no {code} at all -- substituting is a no-op
    # there, which is correct: that host only ever serves the current map.
    resolved = template.replace("{code}", version.code)
    return TileSource(
        name=f"{config.park_id}/{version.code}",
        template=resolved,
        extension=config.tile_extension,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/jpeg,image/*;q=0.8,*/*;q=0.5",
        },
    )
