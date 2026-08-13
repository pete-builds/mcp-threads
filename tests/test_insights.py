"""Insights: the three-shape response trap, and request validation.

The parser tests here are deliberately shape-by-shape AND all-at-once. A parser
that handles only ``total_value`` returns a plausible-looking partial answer for
a mixed response: no exception, no empty list, just two missing metrics. The
``test_a_mixed_response_...`` test is the one that bites for that.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from clients.errors import ThreadsInputError
from clients.insights import (
    EARLIEST_TIMESTAMP,
    coerce_timestamp,
    normalize_metrics,
    parse_insights,
    parse_metric_row,
    validate_account_insights,
    validate_media_insights,
)
from tests.test_client import GRAPH, SEED, seed_volume
from tests.test_tools import build, call

# --- the three shapes, one at a time -----------------------------------

TIME_SERIES_ROW = {
    "name": "views",
    "period": "day",
    "title": "Views",
    "values": [
        {"value": 10, "end_time": "2026-08-10T07:00:00+0000"},
        {"value": 25, "end_time": "2026-08-11T07:00:00+0000"},
    ],
}

TOTAL_VALUE_ROW = {
    "name": "likes",
    "period": "lifetime",
    "title": "Likes",
    "total_value": {"value": 42},
}

LINK_TOTAL_ROW = {
    "name": "clicks",
    "period": "day",
    "link_total_values": [
        {"value": 7, "link_url": "https://brooksnewmedia.com"},
        {"value": 3, "link_url": "https://github.com/pete-builds"},
    ],
}

MEDIA_LIFETIME_ROW = {  # media insights: `values`, lifetime, no end_time
    "name": "views",
    "period": "lifetime",
    "values": [{"value": 311}],
}


def test_time_series_shape():
    parsed = parse_metric_row(TIME_SERIES_ROW)
    assert parsed["kind"] == "time_series"
    assert parsed["value"] == 35  # summed across the window
    assert parsed["series"][1] == {"value": 25, "end_time": "2026-08-11T07:00:00+0000"}


def test_total_value_shape():
    parsed = parse_metric_row(TOTAL_VALUE_ROW)
    assert parsed["kind"] == "total_value"
    assert parsed["value"] == 42


def test_link_total_values_shape():
    parsed = parse_metric_row(LINK_TOTAL_ROW)
    assert parsed["kind"] == "link_total_values"
    assert parsed["value"] == 10
    assert parsed["links"][0]["link_url"] == "https://brooksnewmedia.com"


def test_media_lifetime_values_shape():
    parsed = parse_metric_row(MEDIA_LIFETIME_ROW)
    assert parsed["kind"] == "time_series"
    assert parsed["value"] == 311
    assert parsed["series"] == [{"value": 311, "end_time": None}]


def test_follower_demographics_breakdown_shape():
    row = {
        "name": "follower_demographics",
        "total_value": {
            "breakdowns": [
                {
                    "dimension_keys": ["country"],
                    "results": [
                        {"dimension_values": ["US"], "value": 812},
                        {"dimension_values": ["CA"], "value": 96},
                    ],
                }
            ]
        },
    }
    parsed = parse_metric_row(row)
    assert parsed["kind"] == "breakdown"
    assert parsed["value"] is None
    assert parsed["breakdowns"][0] == {"dimension": {"country": "US"}, "value": 812}


def test_an_unrecognised_shape_is_flagged_not_dropped():
    parsed = parse_metric_row({"name": "mystery", "some_new_key": [1, 2]})
    assert parsed["kind"] == "unknown"
    assert parsed["value"] is None
    assert parsed["raw_keys"] == ["some_new_key"]


# --- all three at once: the test that catches a one-shape parser -------


def test_a_mixed_response_returns_every_metric_not_just_one_shape():
    payload = {"data": [TIME_SERIES_ROW, TOTAL_VALUE_ROW, LINK_TOTAL_ROW]}
    metrics = parse_insights(payload)

    assert set(metrics) == {"views", "likes", "clicks"}
    assert metrics["views"]["kind"] == "time_series"
    assert metrics["likes"]["kind"] == "total_value"
    assert metrics["clicks"]["kind"] == "link_total_values"
    # Every metric produces a usable headline number, whatever its shape.
    assert [metrics[n]["value"] for n in ("views", "likes", "clicks")] == [35, 42, 10]


def test_no_shape_silently_yields_none():
    """A dropped branch shows up as a null value, so assert against that directly."""
    metrics = parse_insights({"data": [TIME_SERIES_ROW, TOTAL_VALUE_ROW, LINK_TOTAL_ROW]})
    assert all(row["value"] is not None for row in metrics.values())
    assert all(row["kind"] != "unknown" for row in metrics.values())


def test_an_empty_data_array_is_empty_not_an_error():
    assert parse_insights({"data": []}) == {}
    assert parse_insights({}) == {}


# --- request validation ------------------------------------------------


def test_metrics_accept_a_list_or_a_comma_string():
    assert normalize_metrics("views, likes", ("views", "likes"), ()) == ["views", "likes"]
    assert normalize_metrics(["VIEWS", "views"], ("views",), ()) == ["views"]


def test_an_unknown_metric_is_rejected_before_the_api_call():
    with pytest.raises(ThreadsInputError, match="Unknown metric"):
        normalize_metrics(["impressions"], ("views",), ("views",))


def test_shares_is_media_only_and_clicks_is_account_only():
    validate_media_insights(["shares"])
    with pytest.raises(ThreadsInputError):
        validate_media_insights(["clicks"])
    validate_account_insights(["clicks"])
    with pytest.raises(ThreadsInputError):
        validate_account_insights(["shares"])


def test_followers_count_rejects_a_time_range():
    with pytest.raises(ThreadsInputError, match="does not support since/until"):
        validate_account_insights(["followers_count"], since="2026-08-01")


def test_follower_demographics_needs_exactly_one_valid_breakdown():
    with pytest.raises(ThreadsInputError, match="requires exactly one breakdown"):
        validate_account_insights(["follower_demographics"])
    with pytest.raises(ThreadsInputError, match="breakdown must be one of"):
        validate_account_insights(["follower_demographics"], breakdown="zodiac")
    out = validate_account_insights(["follower_demographics"], breakdown="country")
    assert out["params"]["breakdown"] == "country"


def test_breakdown_without_demographics_is_rejected():
    with pytest.raises(ThreadsInputError, match="only applies to"):
        validate_account_insights(["views"], breakdown="country")


def test_timestamps_before_2024_04_13_are_rejected():
    with pytest.raises(ThreadsInputError, match="earliest supported timestamp"):
        validate_account_insights(["views"], since=EARLIEST_TIMESTAMP - 1)
    out = validate_account_insights(["views"], since=EARLIEST_TIMESTAMP)
    assert out["params"]["since"] == EARLIEST_TIMESTAMP


def test_until_must_be_after_since():
    with pytest.raises(ThreadsInputError, match="until must be after since"):
        validate_account_insights(["views"], since="2026-08-10", until="2026-08-01")


def test_dates_and_epochs_are_both_accepted():
    assert coerce_timestamp("2024-04-13", field="since") == 1712966400
    assert coerce_timestamp(1712991600, field="since") == 1712991600
    assert coerce_timestamp("1712991600", field="since") == 1712991600
    with pytest.raises(ThreadsInputError):
        coerce_timestamp("last tuesday", field="since")


def test_omitting_the_range_sends_no_since_or_until():
    out = validate_account_insights(["views"])
    assert "since" not in out["params"] and "until" not in out["params"]


# --- through the tools -------------------------------------------------


@respx.mock
async def test_get_account_insights_tool_handles_all_three_shapes(tmp_path):
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    route = respx.get(f"{GRAPH}/77/threads_insights").mock(
        return_value=httpx.Response(
            200, json={"data": [TIME_SERIES_ROW, TOTAL_VALUE_ROW, LINK_TOTAL_ROW]}
        )
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(
        mcp, "get_account_insights", metrics=["views", "likes", "clicks"], since="2026-08-01"
    )
    assert out["data"]["summary"] == {"views": 35, "likes": 42, "clicks": 10}
    params = dict(route.calls[0].request.url.params)
    assert params["metric"] == "views,likes,clicks"
    assert params["since"] == "1785542400"  # 2026-08-01T00:00:00Z
    await client.close()


@respx.mock
async def test_get_post_insights_tool_shapes_the_media_response(tmp_path):
    respx.get(f"{GRAPH}/m1/insights").mock(
        return_value=httpx.Response(
            200, json={"data": [MEDIA_LIFETIME_ROW, TOTAL_VALUE_ROW]}
        )
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "get_post_insights", media_id="m1", metrics=["views", "likes"])
    assert out["data"]["summary"] == {"views": 311, "likes": 42}
    assert out["data"]["media_id"] == "m1"
    await client.close()


@respx.mock
async def test_a_repost_facade_post_returns_an_explained_empty_result(tmp_path):
    """Pete has one on his timeline, so this path WILL be hit live."""
    respx.get(f"{GRAPH}/m-repost/insights").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "get_post_insights", media_id="m-repost")
    assert "data" in out  # empty is data, not an error
    assert out["data"]["metrics"] == {}
    assert "REPOST_FACADE" in out["data"]["note"]
    assert "nested replies" in out["data"]["note"]
    await client.close()


@respx.mock
async def test_insights_require_the_manage_insights_scope(tmp_path):
    route = respx.get(f"{GRAPH}/m1/insights")
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path, granted_scopes=("threads_basic",))
    out = await call(mcp, "get_post_insights", media_id="m1")
    assert out["code"] == "AUTH_FAILED"
    assert out["details"]["missing_scope"] == "threads_manage_insights"
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_a_bad_insight_request_never_reaches_the_api(tmp_path):
    route = respx.get(f"{GRAPH}/77/threads_insights")
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "get_account_insights", metrics=["followers_count"], since="2026-08-01")
    assert out["code"] == "INVALID_INPUT"
    assert route.call_count == 0
    await client.close()
