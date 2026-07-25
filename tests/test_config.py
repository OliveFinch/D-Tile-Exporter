from __future__ import annotations

import json

import pytest

from tilearc.config import (
    DirConfigSource,
    ParkRepository,
    TileBounds,
    parse_bounds_entry,
    parse_park_config,
    parse_version_list,
)
from tilearc.errors import ConfigError

ALL_PARKS = ("wdw", "dlr", "hkdl", "shdr", "dlp", "tdr")


def test_every_park_loads(repo):
    for park_id in ALL_PARKS:
        config = repo.park(park_id)
        assert config.park_id == park_id
        assert config.bounds_by_zoom
        assert config.min_zoom <= config.max_zoom


def test_list_parks(repo):
    assert set(repo.list_parks()) == set(ALL_PARKS)


@pytest.mark.parametrize(
    "park_id,y_scheme",
    [("wdw", "xyz"), ("dlr", "xyz"), ("hkdl", "xyz"), ("dlp", "xyz"), ("tdr", "xyz"), ("shdr", "tms")],
)
def test_y_scheme(repo, park_id, y_scheme):
    assert repo.park(park_id).y_scheme == y_scheme
    assert repo.park(park_id).is_tms == (y_scheme == "tms")


def test_tdr_is_the_only_credentialed_park(repo):
    credentialed = {p for p in ALL_PARKS if repo.park(p).requires_credentials}
    assert credentialed == {"tdr"}


def test_tdr_has_no_public_template(repo):
    assert repo.park("tdr").tile_template is None


def test_dlp_template_has_no_code_placeholder(repo):
    assert "{code}" not in repo.park("dlp").tile_template


def test_active_flag_parsed_from_integers(repo):
    shdr = {v.code: v for v in repo.versions("shdr")}
    assert shdr["7"].active is False   # "active": 0
    assert shdr["18"].active is True   # "active": 1


def test_dlp_jan2026_carries_a_url_override(repo):
    entry = repo.version("dlp", "jan2026")
    assert entry.url is not None
    assert entry.url.startswith("https://pub-")
    # ...and the sibling entry does not.
    assert repo.version("dlp", "current").url is None


def test_unlisted_version_is_synthesised(repo):
    """Old codes drop off the published list but stay fetchable -- that's the point."""
    entry = repo.version("wdw", "999999999")
    assert entry.code == "999999999"
    assert entry.active is False
    assert entry.url is None


def test_wdw_has_many_versions(repo):
    codes = [v.code for v in repo.versions("wdw")]
    assert "47" in codes and "801755166" in codes
    assert len(codes) > 50


# -- parsing edge cases ----------------------------------------------------


def test_bounds_entry_accepts_alternate_shapes():
    expected = TileBounds(1, 2, 3, 4)
    assert parse_bounds_entry({"minX": 1, "maxX": 2, "minY": 3, "maxY": 4}) == expected
    assert parse_bounds_entry({"min_x": 1, "max_x": 2, "min_y": 3, "max_y": 4}) == expected
    assert parse_bounds_entry({"x": [1, 2], "y": [3, 4]}) == expected
    assert parse_bounds_entry([1, 3, 2, 4]) == expected


def test_inverted_bounds_rejected():
    with pytest.raises(ConfigError, match="inverted"):
        TileBounds(10, 5, 0, 1)


def test_snake_case_keys_accepted():
    config = parse_park_config(
        "x",
        {
            "tile_template": "https://e/{z}/{x}/{y}.jpg",
            "min_zoom": 1,
            "max_zoom": 2,
            "y_scheme": "tms",
            "bounds_by_zoom": {"1": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1}},
        },
    )
    assert config.is_tms and config.min_zoom == 1


def test_missing_bounds_is_an_error():
    with pytest.raises(ConfigError, match="boundsByZoom"):
        parse_park_config("x", {"tileTemplate": "t", "minZoom": 1, "maxZoom": 2})


def test_unknown_y_scheme_is_an_error():
    with pytest.raises(ConfigError, match="yScheme"):
        parse_park_config(
            "x",
            {
                "yScheme": "quadkey",
                "boundsByZoom": {"1": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1}},
            },
        )


def test_version_list_accepts_wrapped_arrays():
    entries = parse_version_list("x", {"servers": [{"code": "a"}, {"code": "b", "active": 0}]})
    assert [e.code for e in entries] == ["a", "b"]
    assert entries[1].active is False


def test_missing_file_raises_config_error(tmp_path):
    (tmp_path / "zz").mkdir()
    repo = ParkRepository(DirConfigSource(tmp_path))
    with pytest.raises(ConfigError, match="not found"):
        repo.park("zz")


def test_malformed_json_raises_config_error(tmp_path):
    park = tmp_path / "zz"
    park.mkdir()
    (park / "zz_config.json").write_text("{not json")
    repo = ParkRepository(DirConfigSource(tmp_path))
    with pytest.raises(ConfigError, match="valid JSON"):
        repo.park("zz")


def test_config_dir_accepts_repo_root(tmp_path, fixtures_dir):
    root = tmp_path / "viewer"
    (root / "parks").mkdir(parents=True)
    for child in fixtures_dir.iterdir():
        if child.is_dir():
            target = root / "parks" / child.name
            target.mkdir()
            for f in child.iterdir():
                target.joinpath(f.name).write_text(f.read_text())
    repo = ParkRepository(DirConfigSource(root))
    assert repo.park("wdw").park_id == "wdw"


def test_fixtures_contain_no_real_credentials(fixtures_dir):
    """Guard against a future fixture refresh re-committing live cookies."""
    raw = json.loads((fixtures_dir / "tdr" / "tdr_config.json").read_text())
    for value in raw["cookies"].values():
        assert value.startswith("FIXTURE")
    assert "example-worker" in raw["proxyUrl"]
