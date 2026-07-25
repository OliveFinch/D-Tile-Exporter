"""TDR: URL shape, credentials, expiry, and the shared-quota guard."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tilearc.errors import CredentialsError, CredentialsExpiredError, QuotaError
from tilearc.plan import build_plan
from tilearc.tdr import (
    WORKER_TILE_CAP,
    TdrCredentials,
    build_tdr_source,
    check_worker_quota,
    load_credentials,
    normalise_modes,
)
from tilearc.urls import Outcome

CODE = "20260122183830"


@pytest.fixture
def creds(repo):
    return load_credentials(repo.park("tdr"))


# ---------------------------------------------------------------------------
# url shape
# ---------------------------------------------------------------------------


def test_worker_url_shape(repo, creds):
    source = build_tdr_source(repo.park("tdr"), CODE, "daytime", creds)
    url = source.url(18, 232920, 103240)
    # z-prefixed zoom directory, underscore filename, mode + sid query.
    assert url == (
        "https://example-worker.workers.dev/tdr-tiles/"
        "z18/232920_103240.jpg?mode=daytime&sid=20260122183830"
    )


def test_nighttime_is_a_distinct_tile_set(repo, creds):
    day = build_tdr_source(repo.park("tdr"), CODE, "daytime", creds)
    night = build_tdr_source(repo.park("tdr"), CODE, "nighttime", creds)
    assert day.url(18, 1, 2) != night.url(18, 1, 2)
    assert "mode=nighttime" in night.url(18, 1, 2)


def test_direct_url_shape(repo):
    creds = TdrCredentials(
        origin_base=(
            "https://contents-portal.tokyodisneyresort.jp/limited/map-image/{serverId}/{mode}/"
        ),
        user_agent="TokyoDisneyResortApp/3.11.8",
        cookies={
            "CloudFront-Policy": "p",
            "CloudFront-Signature": "s",
            "CloudFront-Key-Pair-Id": "k",
        },
    )
    source = build_tdr_source(repo.park("tdr"), CODE, "nighttime", creds, direct=True)
    assert source.url(20, 931680, 412960) == (
        "https://contents-portal.tokyodisneyresort.jp/limited/map-image/"
        f"{CODE}/nighttime/z20/931680_412960.jpg"
    )
    assert source.headers["User-Agent"] == "TokyoDisneyResortApp/3.11.8"
    assert source.headers["Referer"].startswith("https://www.tokyodisneyresort.jp")
    assert set(source.cookies) == {
        "CloudFront-Policy",
        "CloudFront-Signature",
        "CloudFront-Key-Pair-Id",
    }


def test_server_id_override_beats_the_version_code(repo, creds):
    creds.server_id = "99999999999999"
    source = build_tdr_source(repo.park("tdr"), CODE, "daytime", creds)
    assert "sid=99999999999999" in source.url(16, 1, 1)


# ---------------------------------------------------------------------------
# missing-tile semantics
# ---------------------------------------------------------------------------


def test_worker_204_is_missing_not_an_error(repo, creds):
    source = build_tdr_source(repo.park("tdr"), CODE, "daytime", creds)
    assert source.classify(204, 0) == Outcome.MISSING
    assert source.classify(200, 0) == Outcome.MISSING  # zero-length body
    assert source.classify(200, 900) == Outcome.OK


def test_direct_403_is_an_auth_failure_not_a_missing_tile(repo):
    """Straight from the origin, 403 means the signature was rejected."""
    creds = TdrCredentials(
        user_agent="ua",
        cookies={
            "CloudFront-Policy": "p",
            "CloudFront-Signature": "s",
            "CloudFront-Key-Pair-Id": "k",
        },
    )
    source = build_tdr_source(repo.park("tdr"), CODE, "daytime", creds, direct=True)
    assert source.classify(403, 0) == Outcome.AUTH
    assert source.classify(404, 0) == Outcome.MISSING


def test_worker_source_is_flagged_as_shared(repo, creds):
    source = build_tdr_source(repo.park("tdr"), CODE, "daytime", creds)
    assert source.uses_shared_proxy is True
    assert "expired" in source.all_missing_hint


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_credentials_load_from_a_file(tmp_path):
    path = tmp_path / "tdr_credentials.json"
    path.write_text(
        json.dumps(
            {
                "proxy_url": "https://w.example/tdr-tiles/",
                "server_id": "123",
                "expires": "2099-01-01T00:00:00Z",
                "cookies": {"CloudFront-Policy": "p"},
            }
        )
    )
    creds = load_credentials(None, path)
    assert creds.proxy_url == "https://w.example/tdr-tiles/"
    assert creds.server_id == "123"
    assert creds.cookies["CloudFront-Policy"] == "p"


def test_missing_explicit_credentials_file_is_an_error(tmp_path):
    with pytest.raises(CredentialsError, match="not found"):
        load_credentials(None, tmp_path / "nope.json")


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"proxy_url": "https://old.example/"}))
    monkeypatch.setenv("TILEARC_TDR_PROXY_URL", "https://new.example/")
    monkeypatch.setenv("TILEARC_TDR_COOKIE_SIGNATURE", "sig-from-env")
    creds = load_credentials(None, path)
    assert creds.proxy_url == "https://new.example/"
    assert creds.cookies["CloudFront-Signature"] == "sig-from-env"


def test_loading_from_the_park_config_warns(repo):
    messages = []
    load_credentials(repo.park("tdr"), warn=messages.append)
    assert any("readable by anyone" in m for m in messages)


def test_expired_credentials_fail_fast_with_a_clear_message(repo):
    creds = load_credentials(repo.park("tdr"))
    creds.expires = "2020-01-01T00:00:00Z"
    with pytest.raises(CredentialsExpiredError, match="expired at"):
        build_tdr_source(repo.park("tdr"), CODE, "daytime", creds)


def test_expiry_warning_fires_inside_the_window():
    soon = datetime.now(timezone.utc) + timedelta(hours=3)
    creds = TdrCredentials(expires=soon.isoformat())
    assert "expire in" in creds.expiry_warning()

    later = datetime.now(timezone.utc) + timedelta(days=30)
    assert TdrCredentials(expires=later.isoformat()).expiry_warning() is None


def test_unparseable_expiry_does_not_block_the_job():
    creds = TdrCredentials(expires="whenever")
    assert creds.expires_at() is None
    creds.check_not_expired()  # must not raise


def test_direct_mode_requires_all_three_cookies(repo):
    creds = TdrCredentials(user_agent="ua", cookies={"CloudFront-Policy": "p"})
    with pytest.raises(CredentialsError, match="CloudFront-Signature"):
        build_tdr_source(repo.park("tdr"), CODE, "daytime", creds, direct=True)


def test_worker_mode_requires_a_proxy_url(repo):
    with pytest.raises(CredentialsError, match="proxy URL"):
        build_tdr_source(repo.park("tdr"), CODE, "daytime", TdrCredentials())


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def test_mode_parsing():
    assert normalise_modes("both") == ["daytime", "nighttime"]
    assert normalise_modes("DAYTIME") == ["daytime"]
    with pytest.raises(CredentialsError):
        normalise_modes("dusk")


def test_both_modes_double_the_job(repo):
    park = repo.park("tdr")
    version = repo.version("tdr", CODE)
    one = build_plan(park, version, max_zoom=17, modes=["daytime"])
    two = build_plan(park, version, max_zoom=17, modes=["daytime", "nighttime"])
    assert one.total_tiles == 2_122
    assert two.total_tiles == 4_244
    assert two.tiles_per_mode == 2_122


# ---------------------------------------------------------------------------
# the shared Cloudflare quota
# ---------------------------------------------------------------------------


def test_small_worker_job_passes():
    assert check_worker_quota(500, forced=False) == []


def test_oversized_worker_job_is_refused():
    with pytest.raises(QuotaError) as excinfo:
        check_worker_quota(137_645, forced=False)
    message = str(excinfo.value)
    assert "100,000 requests/day" in message
    assert "live viewer traffic" in message
    assert "--force" in message


def test_force_allows_it_but_warns_loudly():
    warnings = check_worker_quota(137_645, forced=True)
    assert len(warnings) == 1
    assert "break TDR for everyone" in warnings[0]
    assert "--force" in warnings[0]


def test_full_tdr_job_is_over_the_cap(repo):
    plan = build_plan(repo.park("tdr"), repo.version("tdr", CODE), modes=["daytime"])
    assert plan.total_tiles == 137_645
    assert plan.total_tiles > WORKER_TILE_CAP * 10
