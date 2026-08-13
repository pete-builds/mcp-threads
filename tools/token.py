"""Credential-health and connectivity tools. Built first, per the design spec."""

from __future__ import annotations

from fastmcp import FastMCP

from clients.threads import ThreadsClient
from tools.common import ok, tool_guard


def register_token_tools(mcp: FastMCP, client: ThreadsClient) -> None:
    @mcp.tool()
    @tool_guard
    async def token_status() -> str:
        """Report Threads credential health without touching the API.

        The Threads long-lived token IS the refreshable credential and dies
        permanently if it goes 60 days without a refresh, so this is the
        server's most important tool. Read-only and idempotent; makes no
        network call.

        Returns JSON: ``{"data": {"valid": bool, "days_remaining": float,
        "expires_at": iso8601, "obtained_at": iso8601, "last_refresh_at":
        iso8601|null, "refresh_count": int, "refresh_due_at": iso8601,
        "refresh_due_in_days": float, "source": "seed"|"refresh",
        "store_path": str, "publishes_used_24h": int,
        "publishes_remaining_24h": int}}``. Never returns the token itself.

        Alert when ``days_remaining`` drops below 14 — that means proactive
        refresh (day 45) has not been running.

        Example: ``token_status()``
        """
        return ok(await client.token_status())

    @mcp.tool()
    @tool_guard
    async def whoami() -> str:
        """Return the authenticated Threads profile. Cheap connectivity check.

        Idempotent. One GET to ``/me``; the user ID is cached for the process
        lifetime.

        Returns JSON: ``{"data": {"id": str, "username": str, "name": str|null,
        "threads_profile_picture_url": str|null, "threads_biography":
        str|null}}``.

        Example: ``whoami()``
        """
        return ok(await client.get_profile())
