"""Chain publishing: split long text and post it as a reply chain.

Kept out of :mod:`clients.threads` so the orchestration (which has the
interesting failure semantics) is testable against a fake client.

The rule that matters: each subsequent segment passes ``reply_to_id`` = the
**media ID of the previously published segment**, not the container ID. Using
the container ID produces orphaned replies that never attach to the thread.
"""

from __future__ import annotations

import logging

from clients.text import THREADS_TEXT_LIMIT, split_for_chain
from clients.threads import ThreadsClient, ThreadsError, ThreadsInputError

log = logging.getLogger("mcp-threads.chain")


async def publish_chain(
    client: ThreadsClient,
    text: str,
    *,
    reply_control: str | None = None,
    topic_tag: str | None = None,
    link_attachment: str | None = None,
    reply_to_id: str | None = None,
    limit: int = THREADS_TEXT_LIMIT,
) -> dict:
    """Split ``text`` and publish it as a sequential Threads chain.

    Returns ``{"segments", "published": [...], "complete": bool, "failed_at",
    "error"}``. On a mid-chain failure the already-published IDs are returned
    so the chain can be resumed or cleaned up — never silently swallowed.

    ``topic_tag`` and ``link_attachment`` apply to the first segment only;
    Threads treats them as properties of the root post.
    """
    segments = split_for_chain(text, limit)
    if not segments:
        raise ThreadsInputError("Nothing to post: text is empty.")

    # A chain spends ONE post and N-1 replies, against two separate quotas
    # (250/24h and 1000/24h). If the whole chain hangs off an existing post,
    # every segment is a reply and no post quota is used at all. Checked
    # against Meta's authoritative endpoint, local log only as fallback.
    posts_needed = 0 if reply_to_id else 1
    replies_needed = len(segments) - posts_needed
    budget = await client.quota.require(posts=posts_needed, replies=replies_needed)

    published: list[dict] = []
    parent_id: str | None = reply_to_id

    for index, segment in enumerate(segments):
        is_first = index == 0
        try:
            container = await client.create_container(
                segment,
                reply_to_id=parent_id,
                link_attachment=link_attachment if is_first else None,
                topic_tag=topic_tag if is_first else None,
                reply_control=reply_control if is_first else None,
            )
            result = await client.publish_container(container["creation_id"])
        except ThreadsError as exc:
            log.error(
                "chain failed at segment %d of %d after %d published",
                index + 1,
                len(segments),
                len(published),
            )
            return {
                "segments": len(segments),
                "budget_source": budget["source"],
                "published": published,
                "complete": False,
                "failed_at": index + 1,
                "error": str(exc),
                "error_code": exc.code,
                "details": exc.details,
                "recovery": (
                    "The posts listed in `published` are LIVE. Resume by calling "
                    "post_chain with the remaining text and reply_to_id set to the "
                    "last published media_id, or delete_post them to roll back."
                ),
                "remaining_text": "\n\n".join(segments[index:]),
                "resume_reply_to_id": published[-1]["media_id"] if published else None,
            }

        # The NEXT segment replies to this segment's published media ID.
        parent_id = result["media_id"]
        published.append(
            {
                "index": index + 1,
                "media_id": result["media_id"],
                "permalink": result.get("permalink"),
                "text": segment,
            }
        )

    return {
        "segments": len(segments),
        "quota_spent": {"posts": posts_needed, "replies": replies_needed},
        "budget_source": budget["source"],
        "published": published,
        "complete": True,
        "failed_at": None,
        "error": None,
        "root_media_id": published[0]["media_id"],
        "root_permalink": published[0].get("permalink"),
    }
