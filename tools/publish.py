"""Write-path tools: container creation, publishing, chains, deletion.

``auto_publish_text`` is deliberately not used anywhere in this module. The
two-step create-then-publish separation is the safety boundary that makes a
misfiring agent produce an inert container rather than a live post.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients.chain import publish_chain
from clients.text import count_unique_links, split_for_chain, threads_length
from clients.threads import ThreadsClient, ThreadsInputError
from tools.common import ok, tool_guard


def register_publish_tools(mcp: FastMCP, client: ThreadsClient) -> None:
    @mcp.tool()
    @tool_guard
    async def create_post(
        text: str | None = None,
        reply_to_id: str | None = None,
        image_url: str | None = None,
        alt_text: str | None = None,
        link_attachment: str | None = None,
        topic_tag: str | None = None,
        reply_control: str | None = None,
    ) -> str:
        """Create a Threads container: text, image, or a reply to any post.

        Step 1 of the deliberate two-step publish. The container has no
        timeline effect, so this is safe to call speculatively; call
        ``publish_post`` with the returned ``creation_id`` to commit it.

        Three shapes, one tool:

        * **Text post** — pass ``text``.
        * **Reply** — pass ``text`` plus ``reply_to_id``. Works on **any**
          post, Pete's or anyone else's, not just his own chains. A reply
          spends the separate 1000-per-24h reply quota, not the 250 post quota.
        * **Image post** — pass ``image_url`` (``text`` becomes optional).

        Validated client-side before any API call: the 500 limit (measured in
        UTF-8 bytes so emoji count correctly), the 5-unique-link cap, the
        1000-character ``alt_text`` cap, and that ``image_url`` is a public
        http(s) JPEG/PNG URL.

        Args:
            text: Post body. Max 500 in UTF-8 bytes. Required unless
                ``image_url`` is given. Use ``post_chain`` if longer.
            reply_to_id: Media ID of the post being replied to. Default None
                (a new top-level post).
            image_url: Publicly reachable JPEG or PNG URL. Meta downloads it
                server-side, so a LAN or localhost URL can never work. Meta
                also enforces 8 MB max, at most 10:1 aspect ratio, width 320 to
                1440, sRGB — failures surface via ``get_container_status``.
            alt_text: Accessibility text for the image, max 1000 characters.
                Only valid with ``image_url``.
            link_attachment: URL for a preview card on a text-only post.
                Default None. Counts toward the 5-link cap if it is not
                already present in ``text``.
            topic_tag: 1 to 50 characters, no periods or ampersands. Default None.
            reply_control: One of ``everyone``, ``accounts_you_follow``,
                ``mentioned_only``, ``parent_post_author_only``,
                ``followers_only``. Default None (Threads applies ``everyone``).

        Returns JSON: ``{"data": {"creation_id": str, "media_type":
        "TEXT"|"IMAGE", "is_reply": bool, "requires_status_check": bool,
        "quota_consumed_on_publish": "posts"|"replies", "length": int,
        "link_count": int}}``.

        When ``requires_status_check`` is true, poll ``get_container_status``
        until it reports ``FINISHED`` before calling ``publish_post``: an image
        needs Meta-side processing, and containers expire unpublished after 24
        hours.

        Not idempotent: two calls create two containers (both inert).

        Example: ``create_post(text="Shipped mcp-threads today.",
        reply_control="everyone")``
        """
        return ok(
            await client.create_container(
                text,
                image_url=image_url,
                alt_text=alt_text,
                reply_to_id=reply_to_id,
                link_attachment=link_attachment,
                topic_tag=topic_tag,
                reply_control=reply_control,
            )
        )

    @mcp.tool()
    @tool_guard
    async def get_container_status(container_id: str) -> str:
        """Check whether a container is ready to publish. Read-only.

        Only image containers need this: a TEXT container is publishable the
        moment it exists. Meta downloads and processes ``image_url``
        server-side, and this is the only place the real reason for a failure
        appears.

        Poll roughly once a minute, for no more than 5 minutes. An unpublished
        container expires after 24 hours.

        Args:
            container_id: The ``creation_id`` from ``create_post``.

        Returns JSON: ``{"data": {"container_id": str, "status":
        "EXPIRED"|"ERROR"|"FINISHED"|"IN_PROGRESS"|"PUBLISHED",
        "error_message": str|null, "ready_to_publish": bool, "media_type":
        str|null, "guidance": str}}``.

        ``error_message`` carries Meta's reason on ERROR, for example
        ``INVALID_ASPECT_RATIO``, ``FAILED_DOWNLOADING_VIDEO``, or
        ``INVALID_BIT_RATE``. A failed container cannot be repaired: fix the
        source image and create a new one.

        Example: ``get_container_status(container_id="17851234567890123")``
        """
        return ok(await client.get_container_status(container_id))

    @mcp.tool()
    @tool_guard
    async def publish_post(creation_id: str, is_reply: bool | None = None) -> str:
        """Publish a container created by create_post. This goes live.

        Step 2 of the two-step publish. Charges the right quota: a container
        created with ``reply_to_id`` spends the 1000-replies-per-24h budget,
        anything else spends the 250-posts-per-24h budget. Headroom is checked
        against Meta's authoritative ``threads_publishing_limit`` endpoint, and
        only falls back to the local log if that call fails.

        Args:
            creation_id: The ``creation_id`` returned by ``create_post``.
            is_reply: Override which quota to charge. Default None, which uses
                what ``create_post`` recorded for this container. Only needed
                for a container created before the last server restart.

        Returns JSON: ``{"data": {"media_id": str, "permalink": str|null,
        "quota_kind": "posts"|"replies", "budget_source": str}}``.
        ``permalink`` may be null if the post published but the follow-up
        metadata read failed — the post is still live.

        Not idempotent: Threads rejects a re-publish of the same container,
        which surfaces as ``INVALID_INPUT``.

        Example: ``publish_post(creation_id="17851234567890123")``
        """
        return ok(await client.publish_container(creation_id, is_reply=is_reply))

    @mcp.tool()
    @tool_guard
    async def preview_chain(
        text: str,
        link_attachment: str | None = None,
        reply_to_id: str | None = None,
    ) -> str:
        """Show how text would be split into a chain, without posting anything.

        Read-only and idempotent; makes no network call, which is why the
        budget numbers here are the local fallback. Call
        ``get_publishing_limit`` for Meta's authoritative counts before
        publishing. Use this to review segmentation with Pete first.

        Args:
            text: The full long-form text.
            link_attachment: Optional preview-card URL, counted against the
                5-link cap for the first segment. Default None.
            reply_to_id: If the chain would hang off an existing post, pass it
                here so the quota split is accurate. Default None.

        Returns JSON: ``{"data": {"segments": int, "over_link_limit": bool,
        "quota_needed": {"posts": int, "replies": int}, "preview": [{"index":
        int, "length": int, "text": str}]}}``.

        ``quota_needed`` is the point: a chain spends one post and N-1 replies,
        against two separate 24h quotas (250 and 1000).

        Example: ``preview_chain(text="Paragraph one.\\n\\nParagraph two.")``
        """
        parts = split_for_chain(text)
        posts_needed = 0 if reply_to_id else 1
        return ok(
            {
                "segments": len(parts),
                "link_count": count_unique_links(text, link_attachment),
                "over_link_limit": count_unique_links(text, link_attachment) > 5,
                "quota_needed": {
                    "posts": posts_needed,
                    "replies": max(0, len(parts) - posts_needed),
                },
                "publishes_remaining_24h_local": client.publish_log.remaining("posts"),
                "replies_remaining_24h_local": client.publish_log.remaining("replies"),
                "budget_source": "local_log",
                "budget_note": (
                    "Local fallback counts. Call get_publishing_limit for Meta's "
                    "authoritative quotas before publishing."
                ),
                "preview": [
                    {"index": i + 1, "length": threads_length(p), "text": p}
                    for i, p in enumerate(parts)
                ],
            }
        )

    @mcp.tool()
    @tool_guard
    async def post_chain(
        text: str,
        reply_control: str | None = None,
        topic_tag: str | None = None,
        link_attachment: str | None = None,
        reply_to_id: str | None = None,
    ) -> str:
        """Publish long-form text as a sequential Threads reply chain. Goes live.

        Splits on paragraph boundaries first, then sentence boundaries, then
        word boundaries; never mid-word unless a single word exceeds 500 bytes
        on its own. Length is measured in UTF-8 bytes so emoji count the way
        Threads counts them.

        Each segment after the first replies to the **published media ID** of
        the previous segment. Refuses to start if the projected segment count
        would breach the 250-per-24h budget.

        Args:
            text: The full long-form text.
            reply_control: Applied to the first segment only. Default None.
            topic_tag: Applied to the first segment only. Default None.
            link_attachment: Preview-card URL on the first segment. Default None.
            reply_to_id: Attach the whole chain under an existing post.
                Default None (new top-level chain).

        Returns JSON on success: ``{"data": {"segments": int, "complete": true,
        "published": [{"index", "media_id", "permalink", "text"}],
        "root_media_id": str, "root_permalink": str|null}}``.

        On a mid-chain failure it still returns ``{"data": {...}}`` with
        ``"complete": false``, ``"failed_at": int``, ``"error"``,
        ``"published"`` (the LIVE posts so far), ``"remaining_text"``, and
        ``"resume_reply_to_id"``. Partial success is reported, never swallowed.

        Not idempotent: every call publishes.

        Example: ``post_chain(text="Long post paragraph one.\\n\\nParagraph two.")``
        """
        return ok(
            await publish_chain(
                client,
                text,
                reply_control=reply_control,
                topic_tag=topic_tag,
                link_attachment=link_attachment,
                reply_to_id=reply_to_id,
            )
        )

    @mcp.tool()
    @tool_guard
    async def delete_post(media_id: str, confirm: bool = False) -> str:
        """Delete a published Threads post. DESTRUCTIVE and irreversible.

        **Currently unavailable on this account.** The live token was never
        granted the ``threads_delete`` scope, so this fails before it makes any
        API call, with instructions for granting it. That is a Meta app
        permission gap, not a defect: report it as such rather than retrying.
        Scopes bind at authorization time, so enabling it means adding the
        permission in the Meta App Dashboard and re-running ``bootstrap.py`` to
        mint a new token.

        Gated twice: ``confirm=True`` is required, then the scope is checked,
        then the 100-deletes-per-24h quota.

        Args:
            media_id: The media ID of the post to delete.
            confirm: Must be True. Default False, which returns
                ``INVALID_INPUT`` without contacting the API.

        Returns JSON: ``{"data": {"deleted": true, "media_id": str}}``, or
        ``{"error": ..., "code": "AUTH_FAILED", "details": {"missing_scope":
        "threads_delete", "granted_scopes": [...], "remedy": ...}}``.

        Not idempotent: a second delete of the same ID returns ``NOT_FOUND``
        from Threads.

        Example: ``delete_post(media_id="17851234567890123", confirm=True)``
        """
        if not confirm:
            raise ThreadsInputError(
                "delete_post is destructive and requires confirm=True. "
                "Show the post to the user and get explicit approval first.",
                details={"media_id": media_id},
            )
        return ok(await client.delete_media(media_id))
