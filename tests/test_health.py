"""The ``/healthz`` contract: status codes, boundaries, and secret hygiene.

Uptime Kuma reads the status code, so the code IS the contract. The body is
secondary and must never carry credential material under any state.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastmcp import FastMCP

from clients.health import (
    HTTP_OK,
    HTTP_UNAVAILABLE,
    STATUS_CRITICAL,
    STATUS_OK,
    STATUS_UNSEEDED,
    STATUS_WARNING,
    health_snapshot,
)
from clients.tokenstore import TokenState, TokenStore
from tests.test_client import make_client
from tools.health import register_health_route

DAY = 86400
VERSION = "0.1.0"

#: Distinctive enough that a substring assertion is meaningful, and shaped like
#: a real Threads token so the redaction regexes would have a chance to fire.
SECRET = "THQsecret-token-value-do-not-leak-9876543210"


def store_with(
    tmp_path,
    *,
    days_left: float,
    source: str = "refresh",
    token: str = SECRET,
    cushion_seconds: float = 0.0,
):
    """Write a token to the volume with a controlled time-to-expiry.

    Returns ``(store, now)``. Tests pass ``now`` back into
    :func:`health_snapshot` so ``floor()`` is deterministic — without it the
    milliseconds spent writing the file drop every count by one whole day.
    Route tests cannot inject a clock, so they use ``cushion_seconds`` to sit
    safely inside the day instead.
    """
    now = time.time()
    store = TokenStore(tmp_path / "token.json")
    store.save(
        TokenState(
            access_token=token,
            expires_at=now + days_left * DAY + cushion_seconds,
            obtained_at=now - (60 - days_left) * DAY,
            source=source,
            refresh_count=3,
        )
    )
    return store, now


# --- status codes and states ------------------------------------------


def test_healthy_token_is_200_ok(tmp_path):
    store, now = store_with(tmp_path, days_left=45)
    body, code = health_snapshot(store, version=VERSION, now=now)
    assert code == HTTP_OK
    assert body["status"] == STATUS_OK
    assert body["days_remaining"] == 45
    assert body["token_source"] == "refresh"
    assert body["refresh_count"] == 3
    assert body["version"] == VERSION


def test_fifteen_days_is_still_healthy(tmp_path):
    """Just above the threshold: the refresh window has only now opened."""
    store, now = store_with(tmp_path, days_left=15.5)
    body, code = health_snapshot(store, version=VERSION, now=now)
    assert code == HTTP_OK
    assert body["status"] == STATUS_OK
    assert body["days_remaining"] == 15


def test_exactly_fourteen_days_is_503_warning(tmp_path):
    """The boundary is inclusive: 14 or fewer is unhealthy."""
    store, now = store_with(tmp_path, days_left=14)
    body, code = health_snapshot(store, version=VERSION, now=now)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_WARNING
    assert body["days_remaining"] == 14


def test_boundary_is_applied_to_the_floored_day_count(tmp_path):
    """14.9 days floors to 14, so body and status code agree. Errs early."""
    store, now = store_with(tmp_path, days_left=14.9)
    body, code = health_snapshot(store, version=VERSION, now=now)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_WARNING
    assert body["days_remaining"] == 14


def test_one_day_left_is_503_warning(tmp_path):
    store, now = store_with(tmp_path, days_left=1)
    body, code = health_snapshot(store, version=VERSION, now=now)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_WARNING


def test_expired_token_is_503_critical(tmp_path):
    store, now = store_with(tmp_path, days_left=-3)
    body, code = health_snapshot(store, version=VERSION, now=now)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_CRITICAL
    assert body["days_remaining"] == -3
    assert "expired" in body["detail"]


def test_just_expired_is_critical_not_warning(tmp_path):
    """Zero seconds remaining is dead, not merely expiring."""
    store, now = store_with(tmp_path, days_left=-0.0001)
    body, code = health_snapshot(store, version=VERSION, now=now)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_CRITICAL


def test_unseeded_volume_is_503_unseeded(tmp_path):
    """An unseeded server is not serving; the monitor must not show green."""
    body, code = health_snapshot(TokenStore(tmp_path / "token.json"), version=VERSION)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_UNSEEDED
    assert body["days_remaining"] is None
    assert body["token_source"] is None
    assert body["expires_at"] is None
    assert "bootstrap.py" in body["detail"]


def test_corrupt_token_file_is_503_critical_not_a_traceback(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{ this is not json", encoding="utf-8")
    body, code = health_snapshot(TokenStore(path), version=VERSION)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_CRITICAL
    assert body["days_remaining"] is None
    assert "corrupt" in body["detail"]


def test_binary_garbage_token_file_does_not_raise(tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError — it used to escape."""
    path = tmp_path / "token.json"
    path.write_bytes(b"\xff\xfe\x00\x01binary garbage")
    assert TokenStore(path).load() is None
    body, code = health_snapshot(TokenStore(path), version=VERSION)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_CRITICAL


def test_valid_json_missing_required_keys_is_critical(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"nothing": "useful"}), encoding="utf-8")
    body, code = health_snapshot(TokenStore(path), version=VERSION)
    assert code == HTTP_UNAVAILABLE
    assert body["status"] == STATUS_CRITICAL


def test_seed_source_is_reported_verbatim(tmp_path):
    store, now = store_with(tmp_path, days_left=50, source="seed")
    body, _ = health_snapshot(store, version=VERSION, now=now)
    assert body["token_source"] == "seed"


def test_unknown_source_is_clamped_to_null(tmp_path):
    """The store's ``source`` is free-form JSON; never echo it into the body."""
    store, now = store_with(tmp_path, days_left=50, source=SECRET)
    body, _ = health_snapshot(store, version=VERSION, now=now)
    assert body["token_source"] is None
    assert SECRET not in json.dumps(body)


# --- secret hygiene ----------------------------------------------------


@pytest.mark.parametrize("days_left", [45, 14, 0.5, -5])
def test_no_secret_material_in_body_for_any_live_state(tmp_path, days_left):
    store, now = store_with(tmp_path, days_left=days_left)
    body, _ = health_snapshot(store, version=VERSION, now=now)
    serialized = json.dumps(body)
    assert SECRET not in serialized
    # No prefix of the token either — a leading fragment is still a leak.
    for size in (8, 12, 16, 24):
        assert SECRET[:size] not in serialized
    assert "THQ" not in serialized
    assert "access_token" not in serialized


def test_no_secret_material_in_body_for_corrupt_file(tmp_path):
    """A half-written file can hold a real token; the error path must not echo it."""
    path = tmp_path / "token.json"
    path.write_text(f'{{"access_token": "{SECRET}", "expires_a', encoding="utf-8")
    body, code = health_snapshot(TokenStore(path), version=VERSION)
    assert code == HTTP_UNAVAILABLE
    assert SECRET not in json.dumps(body)
    assert SECRET[:8] not in json.dumps(body)


def test_body_keys_are_a_fixed_set(tmp_path):
    """Pins the response shape: nothing new can drift in unnoticed."""
    expected = {
        "status",
        "days_remaining",
        "token_source",
        "detail",
        "expires_at",
        "refresh_count",
        "version",
    }
    for store in (
        store_with(tmp_path / "a", days_left=45)[0],
        TokenStore(tmp_path / "b" / "token.json"),
    ):
        body, _ = health_snapshot(store, version=VERSION)
        assert set(body) == expected


# --- the route is actually mounted -------------------------------------


def build_app(tmp_path):
    client = make_client(tmp_path, seed=None)
    mcp = FastMCP("Threads-test")
    register_health_route(mcp, client, version=VERSION)
    return mcp.http_app(transport="http")


async def get_healthz(app) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get("/healthz")


async def test_route_serves_200_for_a_healthy_token(tmp_path):
    store_with(tmp_path, days_left=45, cushion_seconds=60)
    resp = await get_healthz(build_app(tmp_path))
    assert resp.status_code == 200
    assert resp.json()["status"] == STATUS_OK
    assert SECRET not in resp.text


async def test_route_serves_503_for_an_unseeded_volume(tmp_path):
    resp = await get_healthz(build_app(tmp_path))
    assert resp.status_code == 503
    assert resp.json()["status"] == STATUS_UNSEEDED


async def test_route_serves_503_for_an_expiring_token(tmp_path):
    store_with(tmp_path, days_left=10, cushion_seconds=60)
    resp = await get_healthz(build_app(tmp_path))
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == STATUS_WARNING
    assert body["days_remaining"] == 10
    assert SECRET not in resp.text


# --- Container healthcheck shim -------------------------------------------
#
# The shim is the thing Docker actually runs. Two properties are load-bearing:
# it must not probe /mcp (which leaks an unreaped transport session per
# request), and a 503 from /healthz must NOT fail the container (a restart
# cannot renew a token, so failing there is a restart loop).


def test_healthcheck_shim_defaults_to_healthz_not_mcp(monkeypatch):
    import healthcheck

    monkeypatch.delenv("MCP_HEALTH_PATH", raising=False)
    seen: dict[str, object] = {}

    def fake_check(port, *, path, healthy_codes, **kw):
        seen.update(port=port, path=path, codes=frozenset(healthy_codes))
        return 0

    monkeypatch.setattr(healthcheck, "check", fake_check)
    assert healthcheck.main() == 0
    assert seen["path"] == "/healthz"
    assert seen["path"] != "/mcp"
    assert seen["port"] == 3726


def test_healthcheck_shim_treats_503_and_401_as_alive(monkeypatch):
    import healthcheck

    monkeypatch.delenv("MCP_HEALTH_PATH", raising=False)
    codes: dict[str, frozenset[int]] = {}

    def fake_check(port, *, path, healthy_codes, **kw):
        codes["v"] = frozenset(healthy_codes)
        return 0

    monkeypatch.setattr(healthcheck, "check", fake_check)
    healthcheck.main()
    assert 503 in codes["v"], "a near-expiry token must not fail the container"
    assert 401 in codes["v"], "MCP_AUTH_REQUIRED must not fail the container"
    assert 200 in codes["v"]
    assert 500 not in codes["v"], "a real server fault must still fail"


def test_healthcheck_shim_honors_env_port_precedence(monkeypatch):
    import healthcheck

    monkeypatch.setenv("MCP_PORT", "3999")
    monkeypatch.setenv("FASTMCP_PORT", "3888")
    monkeypatch.setattr(healthcheck, "check", lambda port, **kw: port)
    assert healthcheck.main() == 3888
    monkeypatch.delenv("FASTMCP_PORT")
    assert healthcheck.main() == 3999


def test_healthcheck_shim_returns_1_on_a_bad_port(monkeypatch):
    import healthcheck

    monkeypatch.setenv("MCP_PORT", "not-a-port")
    assert healthcheck.main() == 1
