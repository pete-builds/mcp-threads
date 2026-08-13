"""Token persistence, the day-45 refresh window, and the publish budget."""

from __future__ import annotations

import json
import os
import time

import pytest

from clients.tokenstore import (
    LONG_LIVED_TTL_SECONDS,
    MIN_REFRESH_AGE_SECONDS,
    REFRESH_MARGIN_SECONDS,
    PublishLog,
    TokenState,
    TokenStore,
)

DAY = 86400


def _state(**kw) -> TokenState:
    now = kw.pop("now", time.time())
    return TokenState(
        access_token=kw.pop("access_token", "THQtoken-1"),
        expires_at=kw.pop("expires_at", now + LONG_LIVED_TTL_SECONDS),
        obtained_at=kw.pop("obtained_at", now),
        **kw,
    )


# --- persistence -------------------------------------------------------


def test_empty_store_returns_none(tmp_path):
    assert TokenStore(tmp_path / "token.json").load() is None


def test_round_trip(tmp_path):
    store = TokenStore(tmp_path / "token.json")
    original = _state(refresh_count=3, source="refresh", last_refresh_at=123.0)
    store.save(original)
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == original.access_token
    assert loaded.refresh_count == 3
    assert loaded.source == "refresh"
    assert loaded.last_refresh_at == 123.0


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    store = TokenStore(tmp_path / "token.json")
    store.save(_state())
    store.save(_state(access_token="THQtoken-2"))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "token.json"]
    assert leftovers == []
    assert store.load().access_token == "THQtoken-2"


def test_saved_file_is_mode_600(tmp_path):
    store = TokenStore(tmp_path / "token.json")
    store.save(_state())
    mode = os.stat(store.path).st_mode & 0o777
    assert mode == 0o600, f"token file is {oct(mode)}, expected 0o600"


def test_corrupt_store_reads_as_empty_not_a_crash(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert TokenStore(path).load() is None


def test_partial_json_reads_as_empty(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"expires_at": 1}), encoding="utf-8")  # no access_token
    assert TokenStore(path).load() is None


def test_save_creates_parent_directory(tmp_path):
    store = TokenStore(tmp_path / "nested" / "deeper" / "token.json")
    store.save(_state())
    assert store.load() is not None


# --- refresh window ----------------------------------------------------


def test_fresh_token_does_not_need_refresh():
    now = time.time()
    assert _state(now=now).needs_refresh(now) is False


def test_token_at_day_44_does_not_need_refresh():
    now = time.time()
    issued = now - 44 * DAY
    st = _state(obtained_at=issued, expires_at=issued + LONG_LIVED_TTL_SECONDS)
    assert st.needs_refresh(now) is False


def test_token_at_day_45_needs_refresh():
    """Proactive refresh fires at day 45, not day 59."""
    now = time.time()
    issued = now - 45 * DAY
    st = _state(obtained_at=issued, expires_at=issued + LONG_LIVED_TTL_SECONDS)
    assert st.needs_refresh(now) is True
    assert round(st.days_remaining(now)) == 15


def test_refresh_margin_is_fifteen_days():
    assert REFRESH_MARGIN_SECONDS == 15 * DAY


def test_token_younger_than_24h_is_never_refreshed():
    """Threads rejects a refresh before the token is 24h old."""
    now = time.time()
    # Contrived: nearly expired but only an hour old (e.g. a short seed TTL).
    st = _state(obtained_at=now - 3600, expires_at=now + 1 * DAY)
    assert st.age_seconds(now) < MIN_REFRESH_AGE_SECONDS
    assert st.needs_refresh(now) is False


def test_expired_token_does_not_claim_it_needs_refresh():
    now = time.time()
    st = _state(obtained_at=now - 61 * DAY, expires_at=now - 1 * DAY)
    assert st.is_expired(now) is True
    assert st.needs_refresh(now) is False  # dead: only re-auth helps


def test_refresh_due_at_is_fifteen_days_before_expiry():
    now = time.time()
    st = _state(now=now)
    assert abs(st.refresh_due_at() - (st.expires_at - 15 * DAY)) < 1


# --- publish budget ----------------------------------------------------


def test_publish_log_starts_empty(tmp_path):
    plog = PublishLog(tmp_path / "publish_log.json")
    assert plog.used() == 0
    assert plog.remaining() == 250
    assert plog.has_budget(250) is True
    assert plog.has_budget(251) is False


def test_publish_log_records_and_persists(tmp_path):
    path = tmp_path / "publish_log.json"
    PublishLog(path).record("posts", 3)
    assert PublishLog(path).used() == 3  # survives re-instantiation
    assert PublishLog(path).remaining() == 247


def test_publish_log_prunes_entries_older_than_24h(tmp_path):
    path = tmp_path / "publish_log.json"
    now = time.time()
    path.write_text(
        json.dumps({"posts": [now - 25 * 3600, now - 100]}), encoding="utf-8"
    )
    plog = PublishLog(path)
    assert plog.used("posts", now=now) == 1


def test_publish_log_blocks_a_chain_that_would_breach_the_cap(tmp_path):
    path = tmp_path / "publish_log.json"
    plog = PublishLog(path)
    plog.record("posts", 248)
    assert plog.has_budget(2) is True
    assert plog.has_budget(3) is False


# --- the three quota kinds, and reading a v1 volume --------------------


def test_publish_log_keeps_the_three_kinds_apart(tmp_path):
    path = tmp_path / "publish_log.json"
    plog = PublishLog(path)
    plog.record("posts", 2)
    plog.record("replies", 5)
    plog.record("deletes", 1)

    reread = PublishLog(path)
    assert (reread.used("posts"), reread.used("replies"), reread.used("deletes")) == (2, 5, 1)
    # Separate ceilings: 250 / 1000 / 100.
    assert reread.remaining("posts") == 248
    assert reread.remaining("replies") == 995
    assert reread.remaining("deletes") == 99


def test_a_v1_flat_list_on_an_existing_volume_is_read_as_posts(tmp_path):
    """The deployed volume already holds the v1 format. It must not reset to zero."""
    path = tmp_path / "publish_log.json"
    now = time.time()
    path.write_text(json.dumps([now - 60, now - 30]), encoding="utf-8")

    plog = PublishLog(path)
    assert plog.used("posts") == 2
    assert plog.used("replies") == 0

    # And a write migrates it forward without losing the history.
    plog.record("replies", 1)
    reread = PublishLog(path)
    assert reread.used("posts") == 2
    assert reread.used("replies") == 1


def test_publish_log_rejects_an_unknown_kind(tmp_path):
    with pytest.raises(ValueError, match="unknown publish log kind"):
        PublishLog(tmp_path / "p.json").record("carousels", 1)
