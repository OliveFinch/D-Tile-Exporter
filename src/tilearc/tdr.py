"""Tokyo Disney Resort: credentials, URL shapes and the shared-quota guard.

TDR has no public tile template. Tiles live at::

    https://contents-portal.tokyodisneyresort.jp/limited/map-image/{serverId}/{mode}/z{z}/{x}_{y}.jpg

note the ``z`` prefix on the zoom directory and the ``{x}_{y}.jpg`` filename --
and they need a spoofed mobile-app User-Agent, a Referer, and three
time-limited CloudFront signed cookies. Reaching them through the viewer's
Cloudflare Worker is the default because that is what the live site does.

Credentials are never stored in this repository. They are read, in order, from:

1. ``--tdr-credentials FILE``
2. ``$TILEARC_TDR_CREDENTIALS``
3. ``./tdr_credentials.json``
4. the park config itself (the viewer keeps them in ``tdr_config.json``)

Option 4 is a convenience for a local checkout, and it prints a warning,
because signed cookies committed to a repo are readable by anyone with access
to it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import USER_AGENT
from .config import ParkConfig
from .errors import CredentialsError, CredentialsExpiredError, QuotaError
from .urls import TileSource

MODES = ("daytime", "nighttime")

#: A full TDR job is ~138k tiles per mode. The viewer's Worker is on
#: Cloudflare's free tier (100k requests/day) and serves live users, so an
#: unguarded archive run would burn the whole day's quota and take TDR down for
#: everyone. Worker-routed jobs are capped well below that unless forced.
WORKER_TILE_CAP = 10_000
WORKER_DAILY_QUOTA = 100_000

DEFAULT_ORIGIN_BASE = (
    "https://contents-portal.tokyodisneyresort.jp/limited/map-image/{serverId}/{mode}/"
)
DEFAULT_REFERER = "https://www.tokyodisneyresort.jp/"

_REQUIRED_COOKIES = ("CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id")

_ENV = {
    "proxy_url": "TILEARC_TDR_PROXY_URL",
    "origin_base": "TILEARC_TDR_ORIGIN_BASE",
    "server_id": "TILEARC_TDR_SERVER_ID",
    "user_agent": "TILEARC_TDR_USER_AGENT",
    "referer": "TILEARC_TDR_REFERER",
    "expires": "TILEARC_TDR_EXPIRES",
}
_ENV_COOKIES = {
    "CloudFront-Policy": "TILEARC_TDR_COOKIE_POLICY",
    "CloudFront-Signature": "TILEARC_TDR_COOKIE_SIGNATURE",
    "CloudFront-Key-Pair-Id": "TILEARC_TDR_COOKIE_KEY_PAIR_ID",
}


@dataclass
class TdrCredentials:
    proxy_url: str | None = None
    origin_base: str = DEFAULT_ORIGIN_BASE
    server_id: str | None = None
    user_agent: str | None = None
    referer: str = DEFAULT_REFERER
    cookies: Mapping[str, str] = field(default_factory=dict)
    expires: str | None = None
    #: Where these came from, for the "credentials expired" message.
    origin: str = "unknown"

    # -- expiry ------------------------------------------------------------

    def expires_at(self) -> datetime | None:
        if not self.expires:
            return None
        text = str(self.expires).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def check_not_expired(self, now: datetime | None = None) -> None:
        deadline = self.expires_at()
        if deadline is None:
            return
        now = now or datetime.now(timezone.utc)
        if now >= deadline:
            raise CredentialsExpiredError(
                f"TDR credentials expired at {deadline.isoformat()} "
                f"(source: {self.origin}). Refresh the CloudFront signed cookies "
                f"and re-run; the job state is preserved, so nothing already "
                f"downloaded will be fetched again."
            )

    def expiry_warning(self, now: datetime | None = None, within_hours: int = 24) -> str | None:
        deadline = self.expires_at()
        if deadline is None:
            return None
        now = now or datetime.now(timezone.utc)
        remaining = (deadline - now).total_seconds() / 3600.0
        if 0 < remaining <= within_hours:
            return f"TDR credentials expire in {remaining:.1f}h ({deadline.isoformat()})"
        return None

    # -- validation --------------------------------------------------------

    def require_direct_fields(self) -> None:
        missing = [name for name in _REQUIRED_COOKIES if not self.cookies.get(name)]
        if missing:
            raise CredentialsError(
                "direct TDR access needs all three CloudFront cookies; missing: "
                + ", ".join(missing)
                + ". Provide them via --tdr-credentials, the TILEARC_TDR_COOKIE_* "
                "environment variables, or route through the worker instead."
            )
        if not self.user_agent:
            raise CredentialsError(
                "direct TDR access needs the mobile-app User-Agent "
                "(TILEARC_TDR_USER_AGENT or 'user_agent' in the credentials file)"
            )


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def _from_mapping(payload: Mapping[str, Any], origin: str) -> TdrCredentials:
    def pick(*names: str, default: Any = None) -> Any:
        for name in names:
            if payload.get(name) not in (None, ""):
                return payload[name]
        return default

    cookies = pick("cookies", default={}) or {}
    if not isinstance(cookies, Mapping):
        raise CredentialsError(f"{origin}: 'cookies' must be an object")

    return TdrCredentials(
        proxy_url=pick("proxy_url", "proxyUrl", "proxy_base", "proxyBase"),
        origin_base=str(
            pick("origin_base", "originBase", "tileBaseUrl", default=DEFAULT_ORIGIN_BASE)
        ),
        server_id=(
            str(pick("server_id", "serverId", "defaultServerId"))
            if pick("server_id", "serverId", "defaultServerId") is not None
            else None
        ),
        user_agent=pick("user_agent", "userAgent"),
        referer=str(pick("referer", "Referer", default=DEFAULT_REFERER)),
        cookies={str(k): str(v) for k, v in cookies.items()},
        expires=pick("expires", "cookieExpires", "expiresAt"),
        origin=origin,
    )


def _merge_env(creds: TdrCredentials) -> TdrCredentials:
    touched = False
    for attr, env_name in _ENV.items():
        value = os.environ.get(env_name)
        if value:
            setattr(creds, attr, value)
            touched = True
    cookies = dict(creds.cookies)
    for cookie_name, env_name in _ENV_COOKIES.items():
        value = os.environ.get(env_name)
        if value:
            cookies[cookie_name] = value
            touched = True
    creds.cookies = cookies
    if touched:
        creds.origin = (
            "environment" if creds.origin == "unknown" else f"{creds.origin} + environment"
        )
    return creds


def load_credentials(
    config: ParkConfig | None = None,
    path: str | Path | None = None,
    *,
    allow_config_fallback: bool = True,
    warn: Any = None,
) -> TdrCredentials:
    """Assemble TDR credentials from file, environment, and (last) the park config."""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path).expanduser())
    elif os.environ.get("TILEARC_TDR_CREDENTIALS"):
        candidates.append(Path(os.environ["TILEARC_TDR_CREDENTIALS"]).expanduser())
    else:
        candidates.append(Path("tdr_credentials.json"))

    creds: TdrCredentials | None = None
    for candidate in candidates:
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise CredentialsError(f"{candidate} is not valid JSON: {exc}") from exc
            creds = _from_mapping(payload, origin=str(candidate))
            break
        if path:  # explicitly requested but absent
            raise CredentialsError(f"credentials file not found: {candidate}")

    if creds is None and config is not None and allow_config_fallback:
        if any(config.raw.get(k) for k in ("cookies", "proxyUrl", "tileBaseUrl")):
            creds = _from_mapping(config.raw, origin=f"{config.park_id}_config.json")
            if creds.cookies and warn:
                warn(
                    f"using CloudFront cookies from {creds.origin}. Signed cookies "
                    "committed to a repository are readable by anyone with access "
                    "to it; consider moving them to a gitignored tdr_credentials.json."
                )

    if creds is None:
        creds = TdrCredentials(origin="defaults")

    creds = _merge_env(creds)
    if config is not None and not creds.origin_base:
        creds.origin_base = str(config.raw.get("tileBaseUrl") or DEFAULT_ORIGIN_BASE)
    return creds


# ---------------------------------------------------------------------------
# tile sources
# ---------------------------------------------------------------------------


def normalise_modes(value: str) -> list[str]:
    text = str(value).strip().lower()
    if text == "both":
        return list(MODES)
    if text not in MODES:
        raise CredentialsError(f"unknown TDR mode {value!r}; expected daytime, nighttime or both")
    return [text]


def build_tdr_source(
    config: ParkConfig,
    version_code: str,
    mode: str,
    credentials: TdrCredentials,
    *,
    direct: bool = False,
) -> TileSource:
    """Build the tile source for one TDR mode.

    The path shape is unusual on both counts the origin cares about: the zoom
    directory is ``z{z}`` and the filename joins x and y with an underscore.
    """
    if mode not in MODES:
        raise CredentialsError(f"unknown TDR mode {mode!r}")
    credentials.check_not_expired()

    # The worker requires a digits-only server id; the version code is the
    # 14-digit timestamp it expects.
    server_id = credentials.server_id or version_code
    tail = "z{z}/{x}_{y}.jpg"

    if direct:
        credentials.require_direct_fields()
        base = credentials.origin_base.replace("{serverId}", server_id).replace("{mode}", mode)
        if not base.endswith("/"):
            base += "/"
        return TileSource(
            name=f"tdr/{version_code}/{mode} (direct)",
            template=base + tail,
            headers={
                "User-Agent": credentials.user_agent or USER_AGENT,
                "Referer": credentials.referer,
                "Accept": "image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
            },
            cookies=dict(credentials.cookies),
            # Direct from the origin, 403 means the signature was rejected --
            # not that the tile is absent. Fail loudly instead of recording
            # 138k phantom "missing" tiles.
            missing_statuses=frozenset({404}),
            auth_statuses=frozenset({401, 403}),
        )

    if not credentials.proxy_url:
        raise CredentialsError(
            "no TDR proxy URL configured. Set 'proxy_url' in the credentials file, "
            "or TILEARC_TDR_PROXY_URL, or pass --direct with full CloudFront cookies."
        )
    base = credentials.proxy_url
    if not base.endswith("/"):
        base += "/"
    return TileSource(
        name=f"tdr/{version_code}/{mode} (worker)",
        template=f"{base}{tail}?mode={mode}&sid={server_id}",
        headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"},
        # The worker maps upstream 403/404 to 204-with-no-body.
        missing_statuses=frozenset({204, 404}),
        auth_statuses=frozenset(),
        uses_shared_proxy=True,
        all_missing_hint=(
            "every tile came back empty (HTTP 204). The worker returns 204 both "
            "for genuinely missing tiles and for upstream 403s, so this usually "
            "means the CloudFront cookies on the worker have expired."
        ),
    )


def check_worker_quota(tile_count: int, *, forced: bool, cap: int = WORKER_TILE_CAP) -> list[str]:
    """Enforce the shared-quota cap for worker-routed jobs.

    Returns warnings to print; raises :class:`QuotaError` when over cap without
    ``--force``.
    """
    warnings: list[str] = []
    if tile_count <= cap:
        return warnings

    share = tile_count / WORKER_DAILY_QUOTA * 100
    message = (
        f"this job needs {tile_count:,} requests through your Cloudflare Worker, "
        f"over the {cap:,}-tile safety cap for proxied jobs.\n"
        f"  The worker is on the free tier ({WORKER_DAILY_QUOTA:,} requests/day) and "
        f"also serves live viewer traffic.\n"
        f"  This run alone would consume ~{share:.0f}% of the daily quota and can "
        f"break TDR for everyone using the map today."
    )
    if not forced:
        raise QuotaError(
            message
            + "\n  Narrow the job (--max-zoom / --bbox), split it across days, "
            "or pass --force if you accept the impact."
        )
    warnings.append(message + "\n  Proceeding because --force was given.")
    return warnings
