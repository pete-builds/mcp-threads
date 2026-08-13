"""Insight tools. Read-only, idempotent, no timeline effect."""

from __future__ import annotations

from fastmcp import FastMCP

from clients.threads import ThreadsClient
from tools.common import ok, tool_guard


def register_insight_tools(mcp: FastMCP, client: ThreadsClient) -> None:
    @mcp.tool()
    @tool_guard
    async def get_post_insights(media_id: str, metrics: list[str] | None = None) -> str:
        """Insights for a single Threads post.

        Read-only and idempotent. Requires the ``threads_manage_insights``
        scope, which this token has.

        Args:
            media_id: Media ID of the post (from ``list_posts`` or a publish).
            metrics: Any of ``views``, ``likes``, ``replies``, ``reposts``,
                ``quotes``, ``shares``. Default: all six.

        Returns JSON: ``{"data": {"media_id": str, "requested_metrics": [str],
        "summary": {metric: int|null}, "metrics": {metric: {"kind": str,
        "value": int|null, ...}}}}``. Read ``summary`` for the headline
        numbers; ``metrics`` carries the shape-specific detail.

        An empty result is normal, not an error: a ``REPOST_FACADE`` post (a
        plain repost of someone else's content) has no insights of its own, and
        a ``note`` field explains it. Media insights also never count nested
        replies.

        Example: ``get_post_insights(media_id="17851234567890123",
        metrics=["views", "likes"])``
        """
        return ok(await client.media_insights(media_id, metrics))

    @mcp.tool()
    @tool_guard
    async def get_account_insights(
        metrics: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        breakdown: str | None = None,
    ) -> str:
        """Insights for the whole @cyb3r_pete profile.

        Read-only and idempotent. Requires ``threads_manage_insights``.

        Args:
            metrics: Any of ``views``, ``likes``, ``replies``, ``reposts``,
                ``quotes``, ``clicks``, ``followers_count``,
                ``follower_demographics``. Default: views, likes, replies,
                reposts, quotes, followers_count.
            since: Start of the window, as a unix timestamp or ``YYYY-MM-DD``.
                Nothing earlier than 2024-04-13 is accepted. Default None.
            until: End of the window, same formats. Default None. Omitting both
                since and until makes Threads default to a 2-day window.
            breakdown: Required with ``follower_demographics`` and rejected
                without it. Exactly one of ``country``, ``city``, ``age``,
                ``gender``.

        Returns JSON: ``{"data": {"user_id": str, "requested_metrics": [str],
        "range": {...}, "summary": {metric: int|null}, "metrics": {metric:
        {"kind": "time_series"|"total_value"|"link_total_values"|"breakdown",
        "value": int|null, ...}}}}``.

        Metric shapes differ: ``views`` is a time series (``series`` of
        per-day points, ``value`` is their sum), ``likes``/``replies``/
        ``reposts``/``quotes``/``followers_count`` are single totals, ``clicks``
        breaks down per link, and ``follower_demographics`` returns dimension
        buckets. ``value`` is the headline number for every shape.

        Constraints Threads enforces: ``followers_count`` and
        ``follower_demographics`` reject since/until, and
        ``follower_demographics`` needs 100+ followers.

        Example: ``get_account_insights(metrics=["views", "likes"],
        since="2026-08-01", until="2026-08-12")``
        """
        return ok(await client.account_insights(metrics, since, until, breakdown))
