"""Threads insights: request validation and the three-shape response parser.

**The trap this module exists for.** ``/insights`` and ``/threads_insights``
return one row per metric, and the row's payload key depends on the metric's
*type*, not on the endpoint:

===================  ==========================================  ==================================
Metric type          Payload key                                 Example metrics
===================  ==========================================  ==================================
Time Series          ``values: [{value, end_time}, ...]``        ``views`` on a user
Total Value          ``total_value: {value}``                    ``likes``, ``replies``, ``reposts``,
                                                                 ``quotes``, ``followers_count``
Link Total Values    ``link_total_values: [{value, link_url}]``  ``clicks``
===================  ==========================================  ==================================

Media insights use the Time Series key with ``period: "lifetime"`` and no
``end_time``. ``follower_demographics`` nests a fourth shape *inside*
``total_value`` as ``breakdowns``.

Reading only one of those keys returns a silent, plausible-looking nothing for
the others: no exception, no empty list, just a missing metric. So the parser
dispatches on all of them and labels anything it does not recognise as
``kind: "unknown"`` with the raw keys attached, rather than dropping it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from clients.errors import ThreadsInputError

#: Metrics valid on a media object. Verified against Meta's docs 2026-08-12.
MEDIA_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")

#: Metrics valid on the authenticated user.
USER_METRICS = (
    "views",
    "likes",
    "replies",
    "reposts",
    "quotes",
    "clicks",
    "followers_count",
    "follower_demographics",
)

DEFAULT_MEDIA_METRICS = MEDIA_METRICS
DEFAULT_USER_METRICS = ("views", "likes", "replies", "reposts", "quotes", "followers_count")

#: These two reject ``since``/``until`` outright.
NO_TIME_RANGE_METRICS = frozenset({"followers_count", "follower_demographics"})

#: ``follower_demographics`` needs exactly one of these.
DEMOGRAPHIC_BREAKDOWNS = frozenset({"country", "city", "age", "gender"})

#: Meta rejects any timestamp before 2024-04-13. Verified 2026-08-12.
EARLIEST_TIMESTAMP = 1712991600

#: Threads counts a repost of someone else's post as this media_product_type,
#: and returns an empty insights array for it.
REPOST_FACADE_NOTE = (
    "Threads returned no insight rows. That is expected for a REPOST_FACADE "
    "post (a plain repost of someone else's content, which carries no insights "
    "of its own) and for media too new to have been processed. Note also that "
    "media insights never include nested replies."
)


# ----------------------------------------------------------------------
# request validation
# ----------------------------------------------------------------------


def normalize_metrics(metrics, allowed: tuple[str, ...], default: tuple[str, ...]) -> list[str]:
    """Accept a list or a comma string, validate against ``allowed``, dedupe."""
    if metrics is None or metrics == "":
        return list(default)
    if isinstance(metrics, str):
        raw = list(metrics.split(","))
    elif isinstance(metrics, (list, tuple)):
        raw = list(metrics)
    else:
        raise ThreadsInputError(
            "metrics must be a list of strings or a comma-separated string.",
            details={"got": type(metrics).__name__},
        )
    out: list[str] = []
    for item in raw:
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in allowed:
            raise ThreadsInputError(
                f"Unknown metric {name!r}. Valid metrics here: {', '.join(allowed)}.",
                details={"metric": name, "allowed": list(allowed)},
            )
        if name not in out:
            out.append(name)
    if not out:
        raise ThreadsInputError(
            "No metrics requested.", details={"allowed": list(allowed)}
        )
    return out


def coerce_timestamp(value, *, field: str) -> int:
    """Accept an epoch int, a numeric string, or ``YYYY-MM-DD`` (UTC midnight)."""
    if isinstance(value, bool):
        raise ThreadsInputError(f"{field} must be a unix timestamp or YYYY-MM-DD.")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ThreadsInputError(
            f"{field} must be a unix timestamp or a YYYY-MM-DD date, got {value!r}.",
            details={"field": field, "value": text},
        ) from exc
    return int(parsed.timestamp())


def validate_account_insights(
    metrics,
    since=None,
    until=None,
    breakdown: str | None = None,
) -> dict:
    """Validate the account-insights request and return API query params.

    Enforces, before spending an API call:

    * ``followers_count`` / ``follower_demographics`` reject ``since``/``until``
    * ``follower_demographics`` requires exactly one valid ``breakdown``
    * ``breakdown`` is meaningless without ``follower_demographics``
    * no timestamp earlier than 2024-04-13
    * ``until`` must be after ``since``
    """
    names = normalize_metrics(metrics, USER_METRICS, DEFAULT_USER_METRICS)

    has_range = since is not None or until is not None
    blocked = [m for m in names if m in NO_TIME_RANGE_METRICS]
    if has_range and blocked:
        raise ThreadsInputError(
            f"{', '.join(blocked)} does not support since/until. Request it in a "
            "separate call, or drop the time range.",
            details={"metrics": blocked},
        )

    if "follower_demographics" in names:
        if not breakdown:
            raise ThreadsInputError(
                "follower_demographics requires exactly one breakdown: "
                + ", ".join(sorted(DEMOGRAPHIC_BREAKDOWNS))
                + ". It also requires the account to have at least 100 followers.",
                details={"allowed_breakdowns": sorted(DEMOGRAPHIC_BREAKDOWNS)},
            )
        if breakdown not in DEMOGRAPHIC_BREAKDOWNS:
            raise ThreadsInputError(
                f"breakdown must be one of {', '.join(sorted(DEMOGRAPHIC_BREAKDOWNS))}, "
                f"got {breakdown!r}.",
                details={"breakdown": breakdown},
            )
    elif breakdown:
        raise ThreadsInputError(
            "breakdown only applies to the follower_demographics metric.",
            details={"breakdown": breakdown, "metrics": names},
        )

    params: dict[str, str | int] = {"metric": ",".join(names)}
    since_ts = until_ts = None
    if since is not None:
        since_ts = coerce_timestamp(since, field="since")
    if until is not None:
        until_ts = coerce_timestamp(until, field="until")
    for label, ts in (("since", since_ts), ("until", until_ts)):
        if ts is not None and ts < EARLIEST_TIMESTAMP:
            raise ThreadsInputError(
                f"{label} is before Threads' earliest supported timestamp "
                f"({EARLIEST_TIMESTAMP}, 2024-04-13). Meta rejects earlier values.",
                details={"field": label, "value": ts, "earliest": EARLIEST_TIMESTAMP},
            )
    if since_ts is not None and until_ts is not None and until_ts <= since_ts:
        raise ThreadsInputError(
            "until must be after since.",
            details={"since": since_ts, "until": until_ts},
        )
    if since_ts is not None:
        params["since"] = since_ts
    if until_ts is not None:
        params["until"] = until_ts
    if breakdown:
        params["breakdown"] = breakdown
    return {"params": params, "metrics": names}


def validate_media_insights(metrics) -> dict:
    names = normalize_metrics(metrics, MEDIA_METRICS, DEFAULT_MEDIA_METRICS)
    return {"params": {"metric": ",".join(names)}, "metrics": names}


# ----------------------------------------------------------------------
# response parsing — the three shapes
# ----------------------------------------------------------------------


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def parse_metric_row(row: dict) -> dict:
    """Normalize one metric row from any of the payload shapes.

    Always returns ``{"kind", "value", ...}`` where ``value`` is the single
    headline number a caller actually wants: the total for a Total Value
    metric, the sum across the window for a Time Series, the sum across links
    for Link Total Values, and ``None`` for a breakdown or an unrecognised
    shape.
    """
    common = {
        "title": row.get("title"),
        "description": row.get("description"),
        "period": row.get("period"),
    }

    link_values = row.get("link_total_values")
    if link_values:
        links = [
            {
                "link_url": item.get("link_url"),
                "value": _as_int(item.get("value")) or 0,
            }
            for item in link_values
            if isinstance(item, dict)
        ]
        return {
            **common,
            "kind": "link_total_values",
            "value": sum(link["value"] for link in links),
            "links": links,
        }

    total = row.get("total_value")
    if total:
        breakdowns = total.get("breakdowns") if isinstance(total, dict) else None
        if breakdowns:
            return {
                **common,
                "kind": "breakdown",
                "value": None,
                "breakdowns": _parse_breakdowns(breakdowns),
            }
        return {
            **common,
            "kind": "total_value",
            "value": _as_int(total.get("value")) if isinstance(total, dict) else None,
        }

    if "values" in row:
        series = [
            {
                "value": _as_int(item.get("value")) or 0,
                "end_time": item.get("end_time"),
            }
            for item in (row.get("values") or [])
            if isinstance(item, dict)
        ]
        return {
            **common,
            "kind": "time_series",
            "value": sum(point["value"] for point in series),
            "series": series,
        }

    return {
        **common,
        "kind": "unknown",
        "value": None,
        "raw_keys": sorted(k for k in row if k not in ("name", "id")),
    }


def _parse_breakdowns(breakdowns) -> list[dict]:
    out: list[dict] = []
    for block in breakdowns:
        if not isinstance(block, dict):
            continue
        keys = block.get("dimension_keys") or []
        for result in block.get("results") or []:
            if not isinstance(result, dict):
                continue
            values = result.get("dimension_values") or []
            out.append(
                {
                    "dimension": dict(zip(keys, values, strict=False)),
                    "value": _as_int(result.get("value")) or 0,
                }
            )
    return out


def parse_insights(payload: dict) -> dict[str, dict]:
    """Map ``{"data": [row, ...]}`` to ``{metric_name: normalized_row}``."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []
    metrics: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "unknown")
        metrics[name] = parse_metric_row(row)
    return metrics


def summarize(metrics: dict[str, dict]) -> dict[str, int | None]:
    """Flat ``{metric: headline value}`` view, for reading at a glance."""
    return {name: row.get("value") for name, row in metrics.items()}
