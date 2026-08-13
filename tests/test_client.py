"""Client behaviour: the 60-day trap, refresh locking, 401 retry, base hosts."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from clients.threads import (
    ThreadsAuthError,
    ThreadsClient,
    ThreadsInputError,
    ThreadsRateLimitError,
)
from clients.tokenstore import LONG_LIVED_TTL_SECONDS, TokenState, TokenStore

DAY = 86400
GRAPH = "https://graph.threads.net/v1.0"
AUTH = "https://graph.threads.com"

SEED = "THQseed-from-dotenv-0000000000"
REFRESHED = "THQrefreshed-on-volume-1111111"


def make_client(tmp_path, seed: str | None = SEED, **kw) -> ThreadsClient:
    return ThreadsClient(
        app_id="app-123",
        app_secret="super-secret",
        data_dir=tmp_path,
        seed_token=seed,
        graph_base=kw.pop("graph_base", GRAPH),
        auth_base=kw.pop("auth_base", AUTH),
        **kw,
    )


def mock_publishing_limit(
    user_id: str = "77",
    *,
    quota_usage: int = 0,
    reply_quota_usage: int = 0,
    delete_quota_usage: int = 0,
):
    """Mock Meta's authoritative quota endpoint, in its real response shape."""
    return respx.get(f"{GRAPH}/{user_id}/threads_publishing_limit").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "quota_usage": quota_usage,
                        "config": {"quota_total": 250, "quota_duration": 86400},
                        "reply_quota_usage": reply_quota_usage,
                        "reply_config": {"quota_total": 1000, "quota_duration": 86400},
                        "delete_quota_usage": delete_quota_usage,
                        "delete_config": {"quota_total": 100, "quota_duration": 86400},
                        "location_search_quota_usage": 0,
                        "location_search_config": {
                            "quota_total": 500,
                            "quota_duration": 86400,
                        },
                    }
                ]
            },
        )
    )


def seed_volume(tmp_path, token: str, *, age_days: float, ttl_days: float = 60.0):
    """Write a token straight to the volume with a controlled age."""
    now = time.time()
    obtained = now - age_days * DAY
    TokenStore(tmp_path / "token.json").save(
        TokenState(
            access_token=token,
            expires_at=obtained + ttl_days * DAY,
            obtained_at=obtained,
            source="refresh",
            refresh_count=1,
        )
    )


# ======================================================================
# THE 60-DAY TRAP — the regression test this whole build exists for
# ======================================================================


@respx.mock
async def test_refreshed_token_survives_restart_and_stale_env_seed_is_ignored(tmp_path):
    """Refresh, throw the client away, rebuild it from the SAME stale .env seed.

    The rebuilt client must use the token written to the volume by the
    refresh, never the dead seed still sitting in .env. A naive port of the
    mcp-spotify pattern passes every other test in this file and fails here,
    silently, 60 days after deploy.
    """
    refresh_route = respx.get(f"{AUTH}/refresh_access_token").mock(
        return_value=httpx.Response(
            200, json={"access_token": REFRESHED, "expires_in": LONG_LIVED_TTL_SECONDS}
        )
    )

    # Day 46: inside the day-45 proactive window.
    seed_volume(tmp_path, SEED, age_days=46)

    client_a = make_client(tmp_path)
    state_a = await client_a.ensure_token()
    assert state_a.access_token == REFRESHED
    assert refresh_route.call_count == 1
    await client_a.close()

    # Container restart. .env still holds the ORIGINAL, now-dead seed.
    client_b = make_client(tmp_path, seed=SEED)
    state_b = await client_b.ensure_token()

    assert state_b.access_token == REFRESHED, (
        "client re-read the stale .env seed instead of the refreshed token on "
        "the volume — this is the 60-day silent death"
    )
    assert state_b.source == "refresh"
    assert state_b.refresh_count == 2
    assert refresh_route.call_count == 1, "restart must not trigger another refresh"
    await client_b.close()


async def test_env_seed_is_adopted_only_when_the_volume_is_empty(tmp_path):
    client = make_client(tmp_path)
    state = client.load_state()
    assert state is not None
    assert state.access_token == SEED
    assert state.source == "seed"
    # Adoption writes through to the volume immediately.
    assert TokenStore(tmp_path / "token.json").load().access_token == SEED
    await client.close()


async def test_volume_token_wins_over_a_different_env_seed(tmp_path):
    seed_volume(tmp_path, REFRESHED, age_days=1)
    client = make_client(tmp_path, seed="THQsome-other-stale-seed-2222")
    assert client.load_state().access_token == REFRESHED
    await client.close()


async def test_no_volume_and_no_seed_raises_auth_failed(tmp_path):
    client = make_client(tmp_path, seed=None)
    with pytest.raises(ThreadsAuthError):
        await client.ensure_token()
    await client.close()


async def test_expired_token_refuses_to_refresh_and_demands_reauth(tmp_path):
    seed_volume(tmp_path, SEED, age_days=61, ttl_days=60)
    client = make_client(tmp_path, seed=None)
    with pytest.raises(ThreadsAuthError, match="expired"):
        await client.ensure_token()
    await client.close()


# ======================================================================
# refresh timing and concurrency
# ======================================================================


@respx.mock
async def test_token_inside_the_window_is_used_without_refreshing(tmp_path):
    route = respx.get(f"{AUTH}/refresh_access_token")
    seed_volume(tmp_path, SEED, age_days=10)
    client = make_client(tmp_path, seed=None)
    state = await client.ensure_token()
    assert state.access_token == SEED
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_concurrent_calls_refresh_exactly_once(tmp_path):
    """Every Threads refresh REPLACES the credential, so a double refresh
    would race two different tokens into the store. The asyncio.Lock plus the
    double-check inside it must collapse N concurrent callers into one call."""
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        await asyncio.sleep(0.02)  # widen the race window
        return httpx.Response(
            200,
            json={
                "access_token": f"{REFRESHED}-{calls['n']}",
                "expires_in": LONG_LIVED_TTL_SECONDS,
            },
        )

    respx.get(f"{AUTH}/refresh_access_token").mock(side_effect=handler)
    seed_volume(tmp_path, SEED, age_days=50)
    client = make_client(tmp_path, seed=None)

    results = await asyncio.gather(*[client.ensure_token() for _ in range(12)])

    assert calls["n"] == 1, f"refreshed {calls['n']} times, expected exactly 1"
    tokens = {r.access_token for r in results}
    assert tokens == {f"{REFRESHED}-1"}
    assert TokenStore(tmp_path / "token.json").load().access_token == f"{REFRESHED}-1"
    await client.close()


@respx.mock
async def test_failed_refresh_keeps_the_old_token_on_the_volume(tmp_path):
    respx.get(f"{AUTH}/refresh_access_token").mock(
        return_value=httpx.Response(400, json={"error": {"message": "nope"}})
    )
    seed_volume(tmp_path, SEED, age_days=50)
    client = make_client(tmp_path, seed=None)
    with pytest.raises(ThreadsAuthError):
        await client.ensure_token()
    # The still-valid old token must not have been clobbered.
    assert TokenStore(tmp_path / "token.json").load().access_token == SEED
    await client.close()


# ======================================================================
# request plumbing
# ======================================================================


@respx.mock
async def test_401_forces_one_refresh_then_retries(tmp_path):
    refresh = respx.get(f"{AUTH}/refresh_access_token").mock(
        return_value=httpx.Response(
            200, json={"access_token": REFRESHED, "expires_in": LONG_LIVED_TTL_SECONDS}
        )
    )
    responses = [
        httpx.Response(401, json={"error": {"message": "token expired"}}),
        httpx.Response(200, json={"id": "999", "username": "pete_builds"}),
    ]
    me = respx.get(f"{GRAPH}/me").mock(side_effect=responses)

    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    profile = await client.get_profile()

    assert profile["username"] == "pete_builds"
    assert refresh.call_count == 1
    assert me.call_count == 2
    # The retry must carry the NEW token, not the rejected one.
    assert dict(me.calls[1].request.url.params)["access_token"] == REFRESHED
    await client.close()


@respx.mock
async def test_both_base_hosts_are_configurable(tmp_path):
    alt_graph = "https://graph.threads.com/v1.0"
    alt_auth = "https://graph.example.test"
    refresh = respx.get(f"{alt_auth}/refresh_access_token").mock(
        return_value=httpx.Response(
            200, json={"access_token": REFRESHED, "expires_in": LONG_LIVED_TTL_SECONDS}
        )
    )
    me = respx.get(f"{alt_graph}/me").mock(
        return_value=httpx.Response(200, json={"id": "1", "username": "u"})
    )
    seed_volume(tmp_path, SEED, age_days=50)
    client = make_client(tmp_path, seed=None, graph_base=alt_graph, auth_base=alt_auth)
    await client.get_profile()
    assert refresh.call_count == 1
    assert me.call_count == 1
    await client.close()


@respx.mock
async def test_429_surfaces_as_rate_limited(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"})
    )
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    with pytest.raises(ThreadsRateLimitError) as exc:
        await client.get_profile()
    assert exc.value.details["retry_after"] == "30"
    await client.close()


@respx.mock
async def test_link_limit_error_from_upstream_maps_to_invalid_input(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    respx.post(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "THREADS_API__LINK_LIMIT_EXCEEDED", "code": 100}},
        )
    )
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    with pytest.raises(ThreadsInputError, match="unique links"):
        await client.create_container("hello")
    await client.close()


# ======================================================================
# client-side validation (never spends an API call)
# ======================================================================


@respx.mock
async def test_over_length_text_rejected_before_any_request(tmp_path):
    route = respx.post(f"{GRAPH}/77/threads")
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    with pytest.raises(ThreadsInputError, match="maximum 500"):
        await client.create_container("\U0001f600" * 200)  # 800 bytes
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_six_links_rejected_before_any_request(tmp_path):
    route = respx.post(f"{GRAPH}/77/threads")
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    text = " ".join(f"https://s{i}.example" for i in range(6))
    with pytest.raises(ThreadsInputError, match="unique links"):
        await client.create_container(text)
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_topic_tag_rules_enforced(tmp_path):
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    with pytest.raises(ThreadsInputError, match="period or ampersand"):
        await client.create_container("hi", topic_tag="a.b")
    with pytest.raises(ThreadsInputError, match="1 to 50"):
        await client.create_container("hi", topic_tag="x" * 51)
    await client.close()


@respx.mock
async def test_invalid_reply_control_rejected(tmp_path):
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    with pytest.raises(ThreadsInputError, match="reply_control"):
        await client.create_container("hi", reply_control="nobody")
    await client.close()


# ======================================================================
# two-step publish
# ======================================================================


@respx.mock
async def test_create_then_publish_never_uses_auto_publish_text(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    create = respx.post(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(200, json={"id": "container-1"})
    )
    publish = respx.post(f"{GRAPH}/77/threads_publish").mock(
        return_value=httpx.Response(200, json={"id": "media-1"})
    )
    respx.get(f"{GRAPH}/media-1").mock(
        return_value=httpx.Response(
            200, json={"id": "media-1", "permalink": "https://threads.net/p/1"}
        )
    )
    mock_publishing_limit()
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)

    container = await client.create_container("hello world")
    assert container["creation_id"] == "container-1"
    params = dict(create.calls[0].request.url.params)
    assert "auto_publish_text" not in params
    assert params["media_type"] == "TEXT"

    result = await client.publish_container("container-1")
    assert result["media_id"] == "media-1"
    assert result["permalink"] == "https://threads.net/p/1"
    assert result["quota_kind"] == "posts"
    assert result["budget_source"] == "api"
    assert dict(publish.calls[0].request.url.params)["creation_id"] == "container-1"
    assert client.publish_log.used() == 1
    await client.close()


@respx.mock
async def test_publish_refuses_when_metas_own_post_quota_is_exhausted(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    mock_publishing_limit(quota_usage=250)
    route = respx.post(f"{GRAPH}/77/threads_publish")
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    # The local log says zero used. Meta says 250. Meta wins.
    assert client.publish_log.used("posts") == 0
    with pytest.raises(ThreadsRateLimitError, match="posts: need 1, 0 left"):
        await client.publish_container("container-1")
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_publish_falls_back_to_the_local_log_when_the_quota_call_fails(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    respx.get(f"{GRAPH}/77/threads_publishing_limit").mock(
        return_value=httpx.Response(500, text="quota endpoint down")
    )
    route = respx.post(f"{GRAPH}/77/threads_publish")
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    client.publish_log.record("posts", 250)
    with pytest.raises(ThreadsRateLimitError, match="posts: need 1, 0 left"):
        await client.publish_container("container-1")
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_permalink_lookup_failure_does_not_look_like_a_failed_publish(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    respx.post(f"{GRAPH}/77/threads_publish").mock(
        return_value=httpx.Response(200, json={"id": "media-9"})
    )
    respx.get(f"{GRAPH}/media-9").mock(return_value=httpx.Response(500))
    seed_volume(tmp_path, SEED, age_days=2)
    client = make_client(tmp_path, seed=None)
    result = await client.publish_container("c-9")
    assert result["media_id"] == "media-9"
    assert result["permalink"] is None
    await client.close()


@respx.mock
async def test_token_status_never_exposes_the_token(tmp_path):
    seed_volume(tmp_path, REFRESHED, age_days=10)
    client = make_client(tmp_path, seed=None)
    status = await client.token_status()
    blob = repr(status)
    assert REFRESHED not in blob
    assert "access_token" not in status
    assert status["valid"] is True
    assert 49 < status["days_remaining"] < 51
    await client.close()
