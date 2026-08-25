"""Read-path tools. All idempotent, no timeline effect."""

from __future__ import annotations

from fastmcp import FastMCP

from clients.threads import ThreadsClient
from tools.common import READ_ONLY, ok, tool_guard


def register_read_tools(mcp: FastMCP, client: ThreadsClient) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @tool_guard
    async def list_posts(limit: int = 10) -> str:
        """List recent posts on the authenticated profile, newest first.

        Read-only and idempotent.

        Args:
            limit: How many posts to return. Default 10, clamped to 1-100.

        Returns JSON: ``{"data": {"count": int, "posts": [{"id": str, "text":
        str|null, "media_type": str, "permalink": str|null, "timestamp":
        iso8601, "shortcode": str|null, "has_replies": bool|null}]}}``.

        Example: ``list_posts(limit=5)``
        """
        posts = await client.list_posts(limit)
        return ok({"count": len(posts), "posts": posts})

    @mcp.tool(annotations=READ_ONLY)
    @tool_guard
    async def get_replies(media_id: str, all_depths: bool = False) -> str:
        """List replies to a post.

        Read-only and idempotent.

        Args:
            media_id: The media ID of the post whose replies you want.
            all_depths: False (default) returns top-level replies via
                ``/replies``. True walks the whole conversation via
                ``/conversation``.

        Returns JSON: ``{"data": {"count": int, "replies": [{"id": str,
        "text": str|null, "username": str|null, "permalink": str|null,
        "timestamp": iso8601, "replied_to_id": str|null, "hide_status":
        str|null}]}}``.

        Example: ``get_replies(media_id="17851234567890123", all_depths=True)``
        """
        replies = await client.get_replies(media_id, all_depths=all_depths)
        return ok({"count": len(replies), "replies": replies})
