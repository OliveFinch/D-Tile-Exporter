"""Loading park configs and version lists.

Source of truth is the viewer repo's ``parks/{id}/`` directory:

    parks/{id}/{id}_config.json        park metadata, zoom range, boundsByZoom
    parks/{id}/{id}_dis_servers.json   the list of known version codes

Nothing about the parks is hardcoded here -- templates, bounds, zoom ranges and
version codes all come from those files, read either from a local checkout
(``--config-dir``) or over HTTP from the live site (``--config-url``).

The readers are deliberately tolerant about key spelling. These files are
hand-maintained and have accumulated a few different conventions; refusing to
load one over ``min_zoom`` vs ``minZoom`` would be unhelpful.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import ConfigError

# ---------------------------------------------------------------------------
# key aliases
# ---------------------------------------------------------------------------

_TEMPLATE_KEYS = ("tileTemplate", "tile_template", "template", "tileUrl", "tileUrlTemplate")
_MIN_ZOOM_KEYS = ("minZoom", "min_zoom", "minzoom")
_MAX_ZOOM_KEYS = ("maxZoom", "max_zoom", "maxzoom")
_Y_SCHEME_KEYS = ("yScheme", "y_scheme", "tileScheme", "scheme")
_BOUNDS_KEYS = ("boundsByZoom", "bounds_by_zoom", "tileBounds", "bounds")
_LABEL_KEYS = ("label", "name", "title", "displayName")
_EXT_KEYS = ("tileExtension", "extension", "format", "tileFormat")
_VERSION_LIST_KEYS = ("servers", "versions", "entries", "items", "dis_servers")
_CODE_KEYS = ("code", "id", "version", "versionCode")
_ACTIVE_KEYS = ("active", "enabled", "isActive")
_URL_OVERRIDE_KEYS = ("url", "tileTemplate", "template", "tileUrl")


def _first(mapping: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TileBounds:
    """An inclusive tile rectangle at one zoom level, in *server* Y space.

    "Server space" means the Y values are exactly what the tile server expects
    in the URL. For a ``yScheme: "tms"`` park they are already TMS rows and
    must not be flipped again -- see :mod:`tilearc.bounds`.
    """

    min_x: int
    max_x: int
    min_y: int
    max_y: int

    def __post_init__(self) -> None:
        if self.min_x > self.max_x or self.min_y > self.max_y:
            raise ConfigError(
                f"inverted tile bounds: x {self.min_x}..{self.max_x}, "
                f"y {self.min_y}..{self.max_y}"
            )

    @property
    def width(self) -> int:
        return self.max_x - self.min_x + 1

    @property
    def height(self) -> int:
        return self.max_y - self.min_y + 1

    @property
    def count(self) -> int:
        return self.width * self.height

    def intersect(self, other: "TileBounds") -> "TileBounds | None":
        min_x, max_x = max(self.min_x, other.min_x), min(self.max_x, other.max_x)
        min_y, max_y = max(self.min_y, other.min_y), min(self.max_y, other.max_y)
        if min_x > max_x or min_y > max_y:
            return None
        return TileBounds(min_x, max_x, min_y, max_y)

    def as_dict(self) -> dict[str, int]:
        return {
            "minX": self.min_x,
            "maxX": self.max_x,
            "minY": self.min_y,
            "maxY": self.max_y,
        }


@dataclass(frozen=True)
class VersionEntry:
    """One entry from ``{id}_dis_servers.json``."""

    code: str
    label: str | None = None
    active: bool = True
    #: Per-version template override. Takes precedence over the park template.
    url: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def display_label(self) -> str:
        return self.label or self.code


@dataclass(frozen=True)
class ParkConfig:
    """Normalised view of ``{id}_config.json``."""

    park_id: str
    label: str
    tile_template: str | None
    min_zoom: int
    max_zoom: int
    y_scheme: str
    bounds_by_zoom: Mapping[int, TileBounds]
    tile_extension: str = "jpg"
    #: True when tiles are unreachable without operator-supplied credentials.
    requires_credentials: bool = False
    #: False for a park that serves one live map and keeps no old versions.
    #: DLP is the case: no selectable servers, always current. An archive of
    #: such a park is identified by when it was taken, not by a version code.
    supports_history: bool = True
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_tms(self) -> bool:
        return self.y_scheme == "tms"

    @property
    def bounds_zooms(self) -> list[int]:
        return sorted(self.bounds_by_zoom)

    def bounds_at(self, zoom: int) -> TileBounds | None:
        return self.bounds_by_zoom.get(zoom)


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


class ConfigSource:
    """Somewhere park JSON can be read from."""

    def describe(self) -> str:  # pragma: no cover - trivial
        raise NotImplementedError

    def read_json(self, relpath: str) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def list_parks(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError


class DirConfigSource(ConfigSource):
    """Reads from a local checkout of the viewer repo."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        if not self.root.is_dir():
            raise ConfigError(f"config dir does not exist: {self.root}")
        # Accept either the repo root or the parks/ directory itself.
        self.parks_root = self.root / "parks" if (self.root / "parks").is_dir() else self.root

    def describe(self) -> str:
        return str(self.parks_root)

    def read_json(self, relpath: str) -> Any:
        path = self.parks_root / relpath
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    def list_parks(self) -> list[str]:
        return sorted(
            child.name
            for child in self.parks_root.iterdir()
            if child.is_dir() and (child / f"{child.name}_config.json").is_file()
        )


class HttpConfigSource(ConfigSource):
    """Reads the same files from the live site.

    Responses are cached for the process lifetime so a single command never
    fetches the same config twice.
    """

    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/parks"):
            self.base_url = f"{self.base_url}/parks"
        self.timeout = timeout
        self._cache: dict[str, Any] = {}

    def describe(self) -> str:
        return self.base_url

    def read_json(self, relpath: str) -> Any:
        if relpath in self._cache:
            return self._cache[relpath]
        import httpx  # imported lazily so offline commands need no network stack

        from . import USER_AGENT

        url = f"{self.base_url}/{relpath}"
        try:
            response = httpx.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ConfigError(f"could not fetch {url}: {exc}") from exc
        if response.status_code != 200:
            raise ConfigError(f"could not fetch {url}: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConfigError(f"{url} is not valid JSON: {exc}") from exc
        self._cache[relpath] = payload
        return payload

    def list_parks(self) -> list[str]:
        raise ConfigError(
            "listing parks is only supported for --config-dir; "
            "pass --park explicitly when using --config-url"
        )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse_bounds_entry(value: Any) -> TileBounds:
    """Parse one ``boundsByZoom`` entry, accepting the shapes seen in the wild."""
    if isinstance(value, Mapping):
        if "x" in value and "y" in value:
            xs, ys = value["x"], value["y"]
            if isinstance(xs, Sequence) and isinstance(ys, Sequence) and len(xs) == len(ys) == 2:
                return TileBounds(int(xs[0]), int(xs[1]), int(ys[0]), int(ys[1]))
        try:
            return TileBounds(
                int(_first(value, ("minX", "min_x", "x_min", "xMin"))),
                int(_first(value, ("maxX", "max_x", "x_max", "xMax"))),
                int(_first(value, ("minY", "min_y", "y_min", "yMin"))),
                int(_first(value, ("maxY", "max_y", "y_max", "yMax"))),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"malformed bounds entry {value!r}: {exc}") from exc
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 4:
        # [minX, minY, maxX, maxY]
        try:
            return TileBounds(int(value[0]), int(value[2]), int(value[1]), int(value[3]))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"malformed bounds entry {value!r}: {exc}") from exc
    raise ConfigError(f"unrecognised bounds entry: {value!r}")


def parse_park_config(park_id: str, payload: Mapping[str, Any]) -> ParkConfig:
    if not isinstance(payload, Mapping):
        raise ConfigError(f"{park_id}: config must be a JSON object")

    raw_bounds = _first(payload, _BOUNDS_KEYS)
    if not isinstance(raw_bounds, Mapping) or not raw_bounds:
        raise ConfigError(f"{park_id}: config has no usable boundsByZoom object")

    bounds: dict[int, TileBounds] = {}
    for key, value in raw_bounds.items():
        try:
            zoom = int(key)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{park_id}: bounds key {key!r} is not a zoom level") from exc
        bounds[zoom] = parse_bounds_entry(value)

    min_zoom = _first(payload, _MIN_ZOOM_KEYS)
    max_zoom = _first(payload, _MAX_ZOOM_KEYS)
    min_zoom = int(min_zoom) if min_zoom is not None else min(bounds)
    max_zoom = int(max_zoom) if max_zoom is not None else max(bounds)
    if min_zoom > max_zoom:
        raise ConfigError(f"{park_id}: minZoom {min_zoom} exceeds maxZoom {max_zoom}")

    y_scheme = str(_first(payload, _Y_SCHEME_KEYS, "xyz")).strip().lower()
    if y_scheme in ("", "slippy", "google", "osm"):
        y_scheme = "xyz"
    if y_scheme not in ("xyz", "tms"):
        raise ConfigError(f"{park_id}: unknown yScheme {y_scheme!r} (expected 'xyz' or 'tms')")

    template = _first(payload, _TEMPLATE_KEYS)
    extension = str(_first(payload, _EXT_KEYS, "jpg")).lstrip(".").lower() or "jpg"

    requires_credentials = bool(
        payload.get("requiresCredentials")
        or payload.get("proxyOnly")
        or payload.get("requiresAuth")
        # No template at all means there is no public URL to build; the only
        # park in that position is TDR, which is credential-gated.
        or (template is None and park_id.lower() == "tdr")
    )

    return ParkConfig(
        park_id=park_id,
        label=str(_first(payload, _LABEL_KEYS, park_id)),
        tile_template=str(template) if template else None,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        y_scheme=y_scheme,
        bounds_by_zoom=bounds,
        tile_extension=extension,
        requires_credentials=requires_credentials,
        supports_history=bool(
            payload.get("supportsHistory", payload.get("supports_history", True))
        ),
        raw=payload,
    )


def parse_version_list(park_id: str, payload: Any) -> list[VersionEntry]:
    if isinstance(payload, Mapping):
        for key in _VERSION_LIST_KEYS:
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            # A bare {code: {...}} mapping is also acceptable.
            if all(isinstance(v, Mapping) for v in payload.values()):
                payload = [{**v, "code": k} for k, v in payload.items()]
            else:
                raise ConfigError(f"{park_id}: version list has no recognised array")
    if not isinstance(payload, list):
        raise ConfigError(f"{park_id}: version list must be an array")

    entries: list[VersionEntry] = []
    for item in payload:
        if isinstance(item, (str, int)):
            entries.append(VersionEntry(code=str(item)))
            continue
        if not isinstance(item, Mapping):
            raise ConfigError(f"{park_id}: unrecognised version entry {item!r}")
        code = _first(item, _CODE_KEYS)
        if code is None:
            raise ConfigError(f"{park_id}: version entry is missing a code: {item!r}")
        active = _first(item, _ACTIVE_KEYS, True)
        # Gotcha #1: a per-version `url` beats the park-level tileTemplate.
        override = _first(item, _URL_OVERRIDE_KEYS)
        label = _first(item, _LABEL_KEYS)
        entries.append(
            VersionEntry(
                code=str(code),
                label=str(label) if label is not None else None,
                # `active` is written as 1/0 in the viewer's files.
                active=bool(active),
                url=str(override) if override else None,
                raw=item,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# façade
# ---------------------------------------------------------------------------


class ParkRepository:
    """Loads and caches park configs / version lists from a :class:`ConfigSource`."""

    def __init__(self, source: ConfigSource) -> None:
        self.source = source
        self._parks: dict[str, ParkConfig] = {}
        self._versions: dict[str, list[VersionEntry]] = {}

    def describe(self) -> str:
        return self.source.describe()

    def list_parks(self) -> list[str]:
        return self.source.list_parks()

    def park(self, park_id: str) -> ParkConfig:
        park_id = park_id.strip().lower()
        if park_id not in self._parks:
            payload = self.source.read_json(f"{park_id}/{park_id}_config.json")
            self._parks[park_id] = parse_park_config(park_id, payload)
        return self._parks[park_id]

    def versions(self, park_id: str) -> list[VersionEntry]:
        park_id = park_id.strip().lower()
        if park_id not in self._versions:
            payload = self.source.read_json(f"{park_id}/{park_id}_dis_servers.json")
            self._versions[park_id] = parse_version_list(park_id, payload)
        return self._versions[park_id]

    def version(self, park_id: str, code: str) -> VersionEntry:
        """Look up one version code, or synthesise an entry if it is unlisted.

        Unlisted codes are allowed on purpose: the whole point of the project is
        that old versions stay reachable after they drop off the published list.
        """
        wanted = str(code).strip()
        for entry in self.versions(park_id):
            if entry.code == wanted:
                return entry
        return VersionEntry(code=wanted, label=None, active=False, url=None, raw={})


def build_repository(
    config_dir: str | Path | None = None,
    config_url: str | None = None,
) -> ParkRepository:
    if config_dir and config_url:
        raise ConfigError("pass only one of --config-dir / --config-url")
    if config_dir:
        return ParkRepository(DirConfigSource(config_dir))
    if config_url:
        return ParkRepository(HttpConfigSource(config_url))
    raise ConfigError(
        "no park config source. Pass --config-dir /path/to/viewer-repo "
        "(or --config-url https://your-site/), or set TILEARC_CONFIG_DIR / "
        "TILEARC_CONFIG_URL."
    )


def iter_active(entries: Iterable[VersionEntry]) -> list[VersionEntry]:
    return [entry for entry in entries if entry.active]
