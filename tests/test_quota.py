"""Quota accounting: Meta's endpoint is authority, the local log is fallback."""

from __future__ import annotations

import httpx
import pytest
import respx

from clients.errors import ThreadsAPIError, ThreadsRateLimitError
from clients.quota import (
    DOCUMENTED_TOTALS,
    PUBLISHING_LIMIT_FIELDS,
    QuotaGate,
    parse_publishing_limit,
)
from clients.tokenstore import PublishLog
from tests.test_client import GRAPH, SEED, make_client, mock_publishing_limit, seed_volume
from tests.test_tools import build, call

# A real-shaped response body.
BODY = {
    "data": [
        {
            "quota_usage": 4,
            "config": {"quota_total": 250, "quota_duration": 86400},
            "reply_quota_usage": 17,
            "reply_config": {"quota_total": 1000, "quota_duration": 86400},
            "delete_quota_usage": 1,
            "delete_config": {"quota_total": 100, "quota_duration": 86400},
            "location_search_quota_usage": 0,
            "location_search_config": {"quota_total": 500, "quota_duration": 86400},
        }
    ]
}


# --- parsing -----------------------------------------------------------


def test_all_four_quota_pairs_are_parsed():
    parsed = parse_publishing_limit(BODY)
    assert parsed["posts"] == {
        "used": 4,
        "quota_total": 250,
        "quota_total_source": "api",
        "remaining": 246,
        "window_seconds": 86400,
        "source": "api",
    }
    assert parsed["replies"]["used"] == 17
    assert parsed["replies"]["remaining"] == 983
    assert parsed["deletes"]["used"] == 1
    assert parsed["deletes"]["remaining"] == 99
    assert parsed["location_searches"]["remaining"] == 500


def test_the_four_kinds_read_four_different_field_pairs():
    """A parser that read `quota_usage` for everything would pass a one-kind test."""
    body = {
        "data": [
            {
                "quota_usage": 1,
                "config": {"quota_total": 250},
                "reply_quota_usage": 2,
                "reply_config": {"quota_total": 1000},
                "delete_quota_usage": 3,
                "delete_config": {"quota_total": 100},
                "location_search_quota_usage": 4,
                "location_search_config": {"quota_total": 500},
            }
        ]
    }
    parsed = parse_publishing_limit(body)
    assert [parsed[k]["used"] for k in ("posts", "replies", "deletes", "location_searches")] == [
        1,
        2,
        3,
        4,
    ]


def test_a_missing_usage_field_is_unknown_not_zero():
    body = {"data": [{"quota_usage": 9, "config": {"quota_total": 250}}]}
    parsed = parse_publishing_limit(body)
    assert parsed["posts"]["used"] == 9
    assert parsed["replies"] is None
    assert parsed["deletes"] is None


def test_a_missing_quota_total_falls_back_to_the_documented_default():
    parsed = parse_publishing_limit({"data": [{"quota_usage": 5}]})
    assert parsed["posts"]["quota_total"] == DOCUMENTED_TOTALS["posts"] == 250
    assert parsed["posts"]["quota_total_source"] == "documented_default"


def test_an_empty_data_array_raises_rather_than_reading_as_zero_usage():
    with pytest.raises(ThreadsAPIError):
        parse_publishing_limit({"data": []})
    with pytest.raises(ThreadsAPIError):
        parse_publishing_limit({})


def test_the_fields_param_asks_for_every_pair():
    assert PUBLISHING_LIMIT_FIELDS == (
        "quota_usage,config,reply_quota_usage,reply_config,"
        "delete_quota_usage,delete_config,"
        "location_search_quota_usage,location_search_config"
    )


# --- the gate ----------------------------------------------------------


async def test_the_api_beats_the_local_log(tmp_path):
    """The whole point: the local log undercounts posts made from the app."""
    log = PublishLog(tmp_path / "p.json")
    log.record("posts", 1)  # this server has published once...

    async def fetch():
        return parse_publishing_limit(BODY)  # ...but Meta has seen four

    snap = await QuotaGate(fetch, log).snapshot()
    assert snap["source"] == "api"
    assert snap["authoritative"] is True
    assert snap["quotas"]["posts"]["used"] == 4
    assert snap["warning"] is None


async def test_the_fallback_is_labelled_loudly(tmp_path):
    log = PublishLog(tmp_path / "p.json")
    log.record("posts", 2)

    async def fetch():
        raise ThreadsAPIError("endpoint down")

    snap = await QuotaGate(fetch, log).snapshot()
    assert snap["source"] == "local_log"
    assert snap["authoritative"] is False
    assert "UNDERCOUNT" in snap["warning"]
    assert snap["quotas"]["posts"]["used"] == 2
    assert snap["quotas"]["posts"]["source"] == "local_log"
    # No local analogue exists for location searches; unknown, not zero.
    assert snap["quotas"]["location_searches"] is None


async def test_a_partial_api_response_is_reported_as_mixed(tmp_path):
    log = PublishLog(tmp_path / "p.json")

    async def fetch():
        return parse_publishing_limit({"data": [{"quota_usage": 7}]})

    snap = await QuotaGate(fetch, log).snapshot()
    assert snap["source"] == "mixed"
    assert snap["quotas"]["posts"]["source"] == "api"
    assert snap["quotas"]["replies"]["source"] == "local_log"


async def test_require_refuses_and_names_every_short_quota(tmp_path):
    async def fetch():
        return parse_publishing_limit(
            {
                "data": [
                    {
                        "quota_usage": 250,
                        "config": {"quota_total": 250},
                        "reply_quota_usage": 999,
                        "reply_config": {"quota_total": 1000},
                    }
                ]
            }
        )

    gate = QuotaGate(fetch, PublishLog(tmp_path / "p.json"))
    with pytest.raises(ThreadsRateLimitError) as exc:
        await gate.require(posts=1, replies=5)
    message = str(exc.value)
    assert "posts: need 1, 0 left" in message
    assert "replies: need 5, 1 left" in message
    # Only two pairs came back, so deletes fell back: "mixed", and both
    # shortfalls above are from the authoritative half.
    assert exc.value.details["budget_source"] == "mixed"
    assert exc.value.details["quotas"]["posts"]["source"] == "api"


async def test_require_passes_when_there_is_headroom(tmp_path):
    async def fetch():
        return parse_publishing_limit(BODY)

    gate = QuotaGate(fetch, PublishLog(tmp_path / "p.json"))
    snap = await gate.require(posts=1, replies=10)
    assert snap["authoritative"] is True


async def test_the_snapshot_is_cached_and_consume_decrements_it(tmp_path):
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return parse_publishing_limit(BODY)

    gate = QuotaGate(fetch, PublishLog(tmp_path / "p.json"))
    await gate.snapshot()
    await gate.snapshot()
    assert calls["n"] == 1  # a 20-segment chain must not cost 20 quota calls

    gate.consume("posts", 1)
    snap = await gate.snapshot()
    assert snap["quotas"]["posts"]["used"] == 5
    assert snap["quotas"]["posts"]["remaining"] == 245

    await gate.snapshot(force=True)
    assert calls["n"] == 2


async def test_consume_records_to_the_local_log_too(tmp_path):
    log = PublishLog(tmp_path / "p.json")

    async def fetch():
        return parse_publishing_limit(BODY)

    gate = QuotaGate(fetch, log)
    gate.consume("replies", 3)
    assert PublishLog(tmp_path / "p.json").used("replies") == 3


# --- through the tool --------------------------------------------------


@respx.mock
async def test_get_publishing_limit_tool_returns_the_api_numbers(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    respx.get(f"{GRAPH}/77/threads_publishing_limit").mock(
        return_value=httpx.Response(200, json=BODY)
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "get_publishing_limit")
    data = out["data"]
    assert data["source"] == "api"
    assert data["authoritative"] is True
    assert data["quotas"]["posts"]["used"] == 4
    assert data["quotas"]["replies"]["quota_total"] == 1000
    await client.close()


@respx.mock
async def test_the_field_set_narrows_until_meta_stops_500ing(tmp_path):
    """Live behaviour on @cyb3r_pete: all four pairs 500, posts+replies works.

    Meta fails the whole response on one unsupported field rather than omitting
    it, so asking for everything gets nothing.
    """
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    seen: list[str] = []

    def responder(request):
        fields = request.url.params["fields"]
        seen.append(fields)
        if "delete_quota_usage" in fields or "location_search" in fields:
            return httpx.Response(500, text="")  # exactly what Meta returns
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "quota_usage": 1,
                        "config": {"quota_total": 250, "quota_duration": 86400},
                        "reply_quota_usage": 0,
                        "reply_config": {"quota_total": 1000, "quota_duration": 86400},
                    }
                ]
            },
        )

    respx.get(f"{GRAPH}/77/threads_publishing_limit").mock(side_effect=responder)
    seed_volume(tmp_path, SEED, age_days=5)
    client = make_client(tmp_path, seed=None)

    snap = await client.publishing_limit()
    assert len(seen) == 3  # widest, then narrower, then posts+replies
    assert snap["quotas"]["posts"]["source"] == "api"
    assert snap["quotas"]["posts"]["used"] == 1
    assert snap["quotas"]["replies"]["source"] == "api"
    # Deletes were never answered: local fallback, and said so.
    assert snap["quotas"]["deletes"]["source"] == "local_log"
    assert snap["source"] == "mixed"
    assert snap["degraded_kinds"] == ["deletes"]
    assert "did not report deletes" in snap["warning"]

    # The working set is remembered, so the next miss is one call, not three.
    await client.quota.snapshot(force=True)
    assert len(seen) == 4
    assert seen[-1] == seen[2]
    await client.close()


@respx.mock
async def test_get_publishing_limit_tool_says_when_it_fell_back(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    respx.get(f"{GRAPH}/77/threads_publishing_limit").mock(
        return_value=httpx.Response(500, text="nope")
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "get_publishing_limit")
    assert out["data"]["source"] == "local_log"
    assert out["data"]["authoritative"] is False
    assert "UNDERCOUNT" in out["data"]["warning"]
    await client.close()


@respx.mock
async def test_publishing_a_reply_charges_the_reply_quota(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    mock_publishing_limit()
    respx.post(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(200, json={"id": "c-1"})
    )
    respx.post(f"{GRAPH}/77/threads_publish").mock(
        return_value=httpx.Response(200, json={"id": "m-1"})
    )
    respx.get(f"{GRAPH}/m-1").mock(return_value=httpx.Response(200, json={"id": "m-1"}))
    seed_volume(tmp_path, SEED, age_days=5)
    client = make_client(tmp_path, seed=None)

    await client.create_container("nice one", reply_to_id="someone-elses-post")
    result = await client.publish_container("c-1")

    assert result["quota_kind"] == "replies"
    assert client.publish_log.used("replies") == 1
    assert client.publish_log.used("posts") == 0
    await client.close()


@respx.mock
async def test_token_status_labels_its_counters_as_the_local_fallback(tmp_path):
    """token_status stays network-free, so it must not pose as authoritative."""
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    client.publish_log.record("posts", 2)
    client.publish_log.record("replies", 6)
    out = await call(mcp, "token_status")
    data = out["data"]
    assert data["budget_source"] == "local_log"
    assert "get_publishing_limit" in data["budget_note"]
    assert data["publishes_used_24h"] == 2
    assert data["replies_used_24h_local"] == 6
    assert respx.calls.call_count == 0  # no network call, ever
    await client.close()
