"""Publish-budget tool. Meta's numbers, not ours."""

from __future__ import annotations

from fastmcp import FastMCP

from clients.threads import ThreadsClient
from tools.common import ok, tool_guard


def register_quota_tools(mcp: FastMCP, client: ThreadsClient) -> None:
    @mcp.tool()
    @tool_guard
    async def get_publishing_limit(force_refresh: bool = False) -> str:
        """Report Meta's authoritative Threads quotas for the last 24 hours.

        Read-only and idempotent. Reads ``threads_publishing_limit``, which
        counts **everything** on the profile, including posts Pete made from
        the Threads app. This is the number to trust before publishing; the
        counters in ``token_status`` are a local log that only sees writes made
        through this server.

        Four independent rolling-24h quotas:

        * ``posts`` (250) — top-level posts
        * ``replies`` (1000) — replies, including every segment of a chain
          after the first
        * ``deletes`` (100)
        * ``location_searches`` (500)

        A chain of N segments spends 1 post and N-1 replies, so a long chain
        can be fine on posts and still be what exhausts replies.

        Args:
            force_refresh: Bypass the 60-second cache. Default False.

        Returns JSON: ``{"data": {"source": "api"|"local_log"|"mixed",
        "authoritative": bool, "warning": str|null, "quotas": {kind: {"used":
        int, "quota_total": int, "remaining": int, "window_seconds": int,
        "source": str} | null}}}``.

        Always check ``source``. ``"api"`` is Meta's own count; anything else
        means the endpoint failed and the numbers are the local fallback, which
        undercounts. ``quotas[kind]`` is null when Meta did not report it,
        which means unknown, not zero.

        Example: ``get_publishing_limit()``
        """
        return ok(await client.publishing_limit(force=force_refresh))
