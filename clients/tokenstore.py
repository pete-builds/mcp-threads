"""Durable token state for the Threads long-lived access token.

**Why this file exists.** Threads does not issue a separate immutable refresh
token. The long-lived access token *is* the refreshable credential, and every
refresh **replaces** it. That makes the credential mutable state, not config.

A naive port of the ``mcp-spotify`` pattern (immutable ``*_REFRESH_TOKEN`` in
``.env``, access token refreshed in memory) works flawlessly for 60 days, then
dies on the next container restart when it re-reads the now-dead seed from
``.env`` — two months after the last code change, with nothing recent to blame.

So: the token lives on a named Docker volume, is written atomically on every
refresh (temp file in the same directory, then :func:`os.replace`), and
``.env`` is a **one-time seed** consumed only when the volume is empty.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

log = logging.getLogger("mcp-threads.tokenstore")

#: Threads long-lived tokens are valid for 60 days.
LONG_LIVED_TTL_SECONDS = 60 * 24 * 3600

#: Refresh proactively at day 45, i.e. once fewer than 15 days remain.
#: Never rely on the last possible moment.
REFRESH_MARGIN_SECONDS = 15 * 24 * 3600

#: Threads refuses to refresh a token younger than 24 hours.
MIN_REFRESH_AGE_SECONDS = 24 * 3600

_SCHEMA_VERSION = 1


@dataclass
class TokenState:
    """The persisted credential and everything needed to reason about it."""

    access_token: str
    expires_at: float
    obtained_at: float
    last_refresh_at: float | None = None
    refresh_count: int = 0
    source: str = "seed"  # "seed" on first adoption, "refresh" thereafter
    schema_version: int = _SCHEMA_VERSION

    # -- derived ------------------------------------------------------

    def seconds_remaining(self, now: float | None = None) -> float:
        return self.expires_at - (now if now is not None else time.time())

    def days_remaining(self, now: float | None = None) -> float:
        return self.seconds_remaining(now) / 86400.0

    def is_expired(self, now: float | None = None) -> bool:
        return self.seconds_remaining(now) <= 0

    def age_seconds(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.obtained_at

    def needs_refresh(self, now: float | None = None) -> bool:
        """True when the token is inside the day-45 proactive refresh window.

        Also requires the token to be at least 24h old, because Threads
        rejects a refresh before then.
        """
        now = now if now is not None else time.time()
        if self.is_expired(now):
            return False  # dead: refresh is impossible, re-auth required
        if self.age_seconds(now) < MIN_REFRESH_AGE_SECONDS:
            return False
        return self.seconds_remaining(now) <= REFRESH_MARGIN_SECONDS

    def refresh_due_at(self) -> float:
        """Epoch seconds at which the proactive refresh becomes due."""
        return self.expires_at - REFRESH_MARGIN_SECONDS


class TokenStore:
    """Atomic JSON token store backed by a file on a named Docker volume."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # -- io -----------------------------------------------------------

    def load(self) -> TokenState | None:
        """Return the persisted state, or ``None`` if the volume is empty.

        A corrupt or unreadable file is treated as empty (and logged) rather
        than crashing the server on boot — the seed path can then recover it.
        ``ValueError`` is caught alongside ``OSError`` because a file holding
        binary garbage raises ``UnicodeDecodeError`` (a ``ValueError``) out of
        ``read_text``, which would otherwise escape at import time via
        ``log_startup_status`` and crash-loop the container.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.error("token store unreadable at %s: %s", self.path, exc)
            return None
        try:
            data = json.loads(raw)
            return TokenState(
                access_token=data["access_token"],
                expires_at=float(data["expires_at"]),
                obtained_at=float(data.get("obtained_at", data["expires_at"] - LONG_LIVED_TTL_SECONDS)),
                last_refresh_at=(
                    float(data["last_refresh_at"])
                    if data.get("last_refresh_at") is not None
                    else None
                ),
                refresh_count=int(data.get("refresh_count", 0)),
                source=str(data.get("source", "seed")),
                schema_version=int(data.get("schema_version", _SCHEMA_VERSION)),
            )
        except (ValueError, KeyError, TypeError) as exc:
            log.error("token store at %s is corrupt (%s); treating as empty", self.path, exc)
            return None

    def save(self, state: TokenState) -> None:
        """Persist ``state`` atomically.

        Writes a temp file in the **same directory** (so ``os.replace`` is a
        same-filesystem rename and therefore atomic), fsyncs it, chmods 600,
        then replaces the target. A crash mid-write leaves the previous good
        token in place; it can never leave a truncated credential behind.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(state), indent=2)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".token-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        # Never log the token itself — only the metadata.
        log.info(
            "token persisted (source=%s, refresh_count=%d, days_remaining=%.1f)",
            state.source,
            state.refresh_count,
            state.days_remaining(),
        )


class PublishLog:
    """Rolling record of write timestamps per quota kind.

    **Fallback only.** Meta's ``threads_publishing_limit`` endpoint is the
    source of truth (see :mod:`clients.quota`); this log exists for when that
    call fails. It structurally undercounts, because it can only ever see
    writes made through this server — never one Pete makes from the Threads
    app. Anything reading it must say so.

    Persisted next to the token on the same named volume so it survives a
    restart. Timestamps only, no post content.

    On-disk format is ``{"posts": [ts, ...], "replies": [...], "deletes": [...]}``.
    A bare list is the v1 format (posts only) and is read transparently, so an
    already-deployed volume keeps its history instead of silently resetting to
    zero used.
    """

    #: Per rolling 24 hours per profile, per Meta's documented defaults.
    LIMITS: ClassVar[dict[str, int]] = {"posts": 250, "replies": 1000, "deletes": 100}
    DAILY_LIMIT = 250
    WINDOW_SECONDS = 24 * 3600
    KINDS = ("posts", "replies", "deletes")

    def __init__(self, path: str | Path, limit: int | None = None):
        self.path = Path(path)
        self.limit = limit if limit is not None else self.DAILY_LIMIT

    def _limit_for(self, kind: str) -> int:
        if kind == "posts":
            return self.limit
        return self.LIMITS[kind]

    def _read(self) -> dict[str, list[float]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {kind: [] for kind in self.KINDS}
        if isinstance(data, list):  # v1: a flat list of post timestamps
            data = {"posts": data}
        if not isinstance(data, dict):
            return {kind: [] for kind in self.KINDS}
        out: dict[str, list[float]] = {}
        for kind in self.KINDS:
            raw = data.get(kind)
            raw = raw if isinstance(raw, list) else []
            out[kind] = [float(t) for t in raw if isinstance(t, (int, float))]
        return out

    def recent(self, kind: str = "posts", *, now: float | None = None) -> list[float]:
        """Timestamps for ``kind`` inside the rolling 24h window."""
        now = now if now is not None else time.time()
        return [t for t in self._read().get(kind, []) if now - t < self.WINDOW_SECONDS]

    def used(self, kind: str = "posts", *, now: float | None = None) -> int:
        return len(self.recent(kind, now=now))

    def remaining(self, kind: str = "posts", *, now: float | None = None) -> int:
        return max(0, self._limit_for(kind) - self.used(kind, now=now))

    def has_budget(self, count: int, kind: str = "posts", *, now: float | None = None) -> bool:
        """True if ``count`` more writes of ``kind`` fit inside the window."""
        return self.remaining(kind, now=now) >= count

    def record(
        self, kind: str = "posts", count: int = 1, *, now: float | None = None
    ) -> None:
        """Append ``count`` timestamps for ``kind``, pruning outside the window."""
        if kind not in self.KINDS:
            raise ValueError(f"unknown publish log kind: {kind}")
        now = now if now is not None else time.time()
        entries = {k: self.recent(k, now=now) for k in self.KINDS}
        entries[kind] = entries[kind] + [now] * count
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".publog-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entries, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
