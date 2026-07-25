"""URL construction, including the per-version override and TDR's path shape."""

from __future__ import annotations

import pytest

from tilearc.errors import ConfigError
from tilearc.urls import Outcome, TileSource, build_source, resolve_template


def test_wdw_url(repo):
    source = build_source(repo.park("wdw"), repo.version("wdw", "801755166"))
    assert source.url(17, 35712, 54688) == (
        "https://cdn6.parksmedia.wdprapps.disney.com/media/maps/prod/"
        "801755166/17/35712/54688.jpg"
    )


def test_wdw_older_version_code(repo):
    source = build_source(repo.park("wdw"), repo.version("wdw", "47"))
    assert "/prod/47/11/555/851.jpg" in source.url(11, 555, 851)


def test_dlr_url_includes_the_disneyland_path_segment(repo):
    source = build_source(repo.park("dlr"), repo.version("dlr", "1"))
    assert "/media/maps/prod/disneyland/1/14/2818/6549.jpg" in source.url(14, 2818, 6549)


def test_hkdl_url(repo):
    source = build_source(repo.park("hkdl"), repo.version("hkdl", "19"))
    assert source.url(14, 13380, 7148).endswith("/prod/hkdl/19/14/13380/7148.jpg")


def test_shdr_url_uses_the_bounds_y_verbatim(repo):
    """SHDR is TMS, but boundsByZoom is already server-space -- no flip here."""
    park = repo.park("shdr")
    source = build_source(park, repo.version("shdr", "18"))
    declared = park.bounds_at(17)
    url = source.url(17, declared.min_x, declared.min_y)
    assert url.endswith("/shdr-baidu-mob-en/18/17/26447/7084.jpg")
    assert "/124051.jpg" not in url  # the flipped value


# ---------------------------------------------------------------------------
# the per-version url override (gotcha #1)
# ---------------------------------------------------------------------------


def test_dlp_default_version_uses_the_park_template(repo):
    park = repo.park("dlp")
    source = build_source(park, repo.version("dlp", "current"))
    assert source.url(15, 16624, 11264) == (
        "https://media.disneylandparis.com/mapTiles/images/15/16624/11264.jpg"
    )


def test_dlp_jan2026_overrides_the_park_template(repo):
    park = repo.park("dlp")
    version = repo.version("dlp", "jan2026")

    assert resolve_template(park, version) == version.url
    assert resolve_template(park, version) != park.tile_template

    source = build_source(park, version)
    url = source.url(15, 16624, 11264)
    assert url.startswith("https://pub-")
    assert url.endswith("/dlp/15/16624/11264.jpg")
    assert "media.disneylandparis.com" not in url


def test_override_wins_for_any_park(repo):
    """Not DLP-specific: the override applies wherever a version declares one."""
    from tilearc.config import VersionEntry

    park = repo.park("wdw")
    version = VersionEntry(code="47", url="https://mirror.example/{code}/{z}/{x}/{y}.jpg")
    source = build_source(park, version)
    assert source.url(11, 555, 851) == "https://mirror.example/47/11/555/851.jpg"


def test_park_with_no_template_and_no_override_is_an_error(repo):
    with pytest.raises(ConfigError, match="nothing to fetch"):
        build_source(repo.park("tdr"), repo.version("tdr", "20260122183830"))


@pytest.mark.parametrize(
    "template,message",
    [
        ("https://e/{z}/{x}.jpg", r"\{y\}"),
        ("https://e/{x}/{y}.jpg", r"\{z\}"),
        ("https://e/{z}/{x}/{y}/{q}.jpg", "unsupported"),
    ],
)
def test_bad_templates_rejected(repo, template, message):
    from tilearc.config import VersionEntry

    with pytest.raises(ConfigError, match=message):
        build_source(repo.park("wdw"), VersionEntry(code="1", url=template))


# ---------------------------------------------------------------------------
# response classification
# ---------------------------------------------------------------------------


def test_cdn_source_treats_403_and_404_as_missing():
    source = TileSource(name="t", template="https://e/{z}/{x}/{y}.jpg")
    assert source.classify(200, 1234) == Outcome.OK
    assert source.classify(403, 0) == Outcome.MISSING
    assert source.classify(404, 0) == Outcome.MISSING
    assert source.classify(429, 0) == Outcome.RETRY
    assert source.classify(503, 0) == Outcome.RETRY


def test_empty_200_counts_as_missing():
    source = TileSource(name="t", template="https://e/{z}/{x}/{y}.jpg")
    assert source.classify(200, 0) == Outcome.MISSING


def test_auth_statuses_take_precedence_over_missing():
    source = TileSource(
        name="t",
        template="https://e/{z}/{x}/{y}.jpg",
        missing_statuses=frozenset({403, 404}),
        auth_statuses=frozenset({403}),
    )
    assert source.classify(403, 0) == Outcome.AUTH
    assert source.classify(404, 0) == Outcome.MISSING
