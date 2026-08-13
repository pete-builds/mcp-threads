"""Chain publishing: reply_to_id wiring and partial-failure reporting."""

from __future__ import annotations

import pytest

from clients.chain import publish_chain
from clients.quota import QuotaGate
from clients.text import THREADS_TEXT_LIMIT
from clients.threads import ThreadsAPIError, ThreadsInputError, ThreadsRateLimitError
from clients.tokenstore import PublishLog


def api_quota(posts_used=0, replies_used=0, deletes_used=0):
    """A parsed quota snapshot as :func:`parse_publishing_limit` would return it."""
    def view(used, total):
        return {
            "used": used,
            "quota_total": total,
            "quota_total_source": "api",
            "remaining": max(0, total - used),
            "window_seconds": 86400,
            "source": "api",
        }

    return {
        "posts": view(posts_used, 250),
        "replies": view(replies_used, 1000),
        "deletes": view(deletes_used, 100),
        "location_searches": view(0, 500),
    }


class FakeClient:
    """Records every create/publish call so the chain wiring can be asserted."""

    def __init__(self, tmp_path, fail_on_publish: int | None = None, quota=None):
        self.creates: list[dict] = []
        self.publishes: list[str] = []
        self.publish_log = PublishLog(tmp_path / "publish_log.json")
        # ``None`` makes the quota endpoint fail, exercising the local-log
        # fallback; pass ``api_quota(...)`` to exercise the authoritative path.
        self._api_quota = quota
        self.quota = QuotaGate(self._fetch_quota, self.publish_log)
        self._fail_on_publish = fail_on_publish
        self._n = 0

    async def _fetch_quota(self):
        if self._api_quota is None:
            raise ThreadsAPIError("quota endpoint unavailable in this fake")
        return self._api_quota

    async def create_container(self, text, **kw):
        self.creates.append({"text": text, **kw})
        return {
            "creation_id": f"container-{len(self.creates)}",
            "length": len(text),
            "link_count": 0,
            "is_reply": bool(kw.get("reply_to_id")),
            "media_type": "TEXT",
        }

    async def publish_container(self, creation_id, *, is_reply=None):
        self._n += 1
        if self._fail_on_publish is not None and self._n == self._fail_on_publish:
            raise ThreadsAPIError("upstream exploded", details={"status": 500})
        self.publishes.append(creation_id)
        kind = "replies" if (is_reply or self._n > 1) else "posts"
        self.quota.consume(kind, 1)
        return {
            "media_id": f"media-{self._n}",
            "permalink": f"https://threads.net/p/{self._n}",
            "quota_kind": kind,
        }


def long_text(paragraphs: int = 4) -> str:
    para = ("word " * 90).strip()  # ~450 bytes, one paragraph per segment
    return "\n\n".join(f"P{i} {para}" for i in range(paragraphs))


async def test_single_segment_chain_has_no_reply_to_id(tmp_path):
    fake = FakeClient(tmp_path)
    result = await publish_chain(fake, "short post")
    assert result["segments"] == 1
    assert result["complete"] is True
    assert fake.creates[0]["reply_to_id"] is None


async def test_each_segment_replies_to_the_previous_published_media_id(tmp_path):
    """Not the container ID. Using the container ID orphans every reply."""
    fake = FakeClient(tmp_path)
    result = await publish_chain(fake, long_text(4))

    assert result["segments"] == 4
    assert result["complete"] is True
    assert fake.creates[0]["reply_to_id"] is None
    assert fake.creates[1]["reply_to_id"] == "media-1"
    assert fake.creates[2]["reply_to_id"] == "media-2"
    assert fake.creates[3]["reply_to_id"] == "media-3"
    # Explicitly: no container ID was ever used as a parent.
    parents = [c["reply_to_id"] for c in fake.creates[1:]]
    assert not any(p.startswith("container-") for p in parents)


async def test_first_segment_carries_the_metadata_and_the_rest_do_not(tmp_path):
    fake = FakeClient(tmp_path)
    await publish_chain(
        fake,
        long_text(3),
        reply_control="followers_only",
        topic_tag="homelab",
        link_attachment="https://example.test",
    )
    assert fake.creates[0]["reply_control"] == "followers_only"
    assert fake.creates[0]["topic_tag"] == "homelab"
    assert fake.creates[0]["link_attachment"] == "https://example.test"
    for c in fake.creates[1:]:
        assert c["reply_control"] is None
        assert c["topic_tag"] is None
        assert c["link_attachment"] is None


async def test_chain_can_hang_off_an_existing_post(tmp_path):
    fake = FakeClient(tmp_path)
    await publish_chain(fake, long_text(2), reply_to_id="existing-media-42")
    assert fake.creates[0]["reply_to_id"] == "existing-media-42"
    assert fake.creates[1]["reply_to_id"] == "media-1"


async def test_mid_chain_failure_returns_the_ids_already_published(tmp_path):
    fake = FakeClient(tmp_path, fail_on_publish=3)
    result = await publish_chain(fake, long_text(5))

    assert result["complete"] is False
    assert result["failed_at"] == 3
    assert len(result["published"]) == 2
    assert [p["media_id"] for p in result["published"]] == ["media-1", "media-2"]
    assert result["error_code"] == "UPSTREAM_DOWN"
    assert result["resume_reply_to_id"] == "media-2"
    assert result["remaining_text"]
    assert "LIVE" in result["recovery"]


async def test_failure_on_the_very_first_segment_reports_nothing_published(tmp_path):
    fake = FakeClient(tmp_path, fail_on_publish=1)
    result = await publish_chain(fake, long_text(3))
    assert result["complete"] is False
    assert result["failed_at"] == 1
    assert result["published"] == []
    assert result["resume_reply_to_id"] is None


async def test_chain_spends_one_post_and_n_minus_one_replies(tmp_path):
    """The correctness fix: a chain is NOT N posts. It is 1 post + N-1 replies."""
    fake = FakeClient(tmp_path, quota=api_quota())
    result = await publish_chain(fake, long_text(4))
    assert result["quota_spent"] == {"posts": 1, "replies": 3}
    assert result["budget_source"] == "api"
    assert fake.publish_log.used("posts") == 1
    assert fake.publish_log.used("replies") == 3


async def test_a_chain_under_an_existing_post_spends_no_post_quota(tmp_path):
    fake = FakeClient(tmp_path, quota=api_quota())
    result = await publish_chain(fake, long_text(3), reply_to_id="existing-42")
    assert result["quota_spent"] == {"posts": 0, "replies": 3}


async def test_chain_refuses_when_the_api_says_post_quota_is_exhausted(tmp_path):
    fake = FakeClient(tmp_path, quota=api_quota(posts_used=250))
    with pytest.raises(ThreadsRateLimitError, match="posts: need 1, 0 left"):
        await publish_chain(fake, long_text(5))
    assert fake.creates == []  # refused before creating a single container


async def test_chain_refuses_on_the_reply_quota_the_old_code_never_checked(tmp_path):
    """248 posts used is fine for a 5-segment chain; 998 replies used is not."""
    fake = FakeClient(tmp_path, quota=api_quota(posts_used=248, replies_used=998))
    with pytest.raises(ThreadsRateLimitError, match="replies: need 4, 2 left"):
        await publish_chain(fake, long_text(5))
    assert fake.creates == []


async def test_a_chain_that_only_the_old_post_only_check_would_have_blocked(tmp_path):
    """248 posts used used to refuse a 5-segment chain. It should not: 1 post is free."""
    fake = FakeClient(tmp_path, quota=api_quota(posts_used=248))
    result = await publish_chain(fake, long_text(5))
    assert result["complete"] is True


async def test_chain_falls_back_to_the_local_log_when_the_api_is_down(tmp_path):
    fake = FakeClient(tmp_path, quota=None)  # quota endpoint raises
    fake.publish_log.record("posts", 250)
    with pytest.raises(ThreadsRateLimitError, match="posts: need 1, 0 left"):
        await publish_chain(fake, long_text(3))
    fake2 = FakeClient(tmp_path / "b", quota=None)
    result = await publish_chain(fake2, long_text(2))
    assert result["budget_source"] == "local_log"


async def test_empty_text_is_rejected(tmp_path):
    fake = FakeClient(tmp_path)
    with pytest.raises(ThreadsInputError):
        await publish_chain(fake, "   ")


async def test_every_published_segment_is_within_the_byte_limit(tmp_path):
    fake = FakeClient(tmp_path)
    text = "\U0001f600 " * 300  # 300 emoji = 1500 bytes
    result = await publish_chain(fake, text)
    assert result["complete"] is True
    assert result["segments"] >= 3
    for create in fake.creates:
        assert len(create["text"].encode("utf-8")) <= THREADS_TEXT_LIMIT
