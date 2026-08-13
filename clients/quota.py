"""Publish budget, sourced from Meta's authoritative quota endpoint.

**Why this file exists.** The first cut of this server tracked the publish
budget in ``publish_log.json`` on the data volume. That log only ever sees
publishes *this container* made, so it undercounts every post Pete makes from
the Threads app or any other client, and it knew nothing at all about replies
or deletes — which sit on their own separate quotas.

Meta exposes the real numbers::

    GET /{threads-user-id}/threads_publishing_limit
        ?fields=quota_usage,config,reply_quota_usage,reply_config,
                delete_quota_usage,delete_config,
                location_search_quota_usage,location_search_config

The response is ``{"data": [ {...one row of four usage/config pairs...} ]}``.

The API is the source of truth for every budget decision. The local log stays
as a **fallback only**, used when the API call fails, and every response says
plainly which source produced it. A silent fallback would be worse than no
budget check at all: it reads as authority it does not have.

The four quotas are independent. In particular a chain of N segments spends
**one** post and **N-1 replies**, so a long chain can be perfectly fine against
the 250/24h post quota and still be the thing that exhausts replies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from clients.errors import ThreadsAPIError, ThreadsRateLimitError

log = logging.getLogger("mcp-threads.quota")

#: Internal kind -> (usage field, config field) on the API row.
FIELD_MAP: dict[str, tuple[str, str]] = {
    "posts": ("quota_usage", "config"),
    "replies": ("reply_quota_usage", "reply_config"),
    "deletes": ("delete_quota_usage", "delete_config"),
    "location_searches": ("location_search_quota_usage", "location_search_config"),
}

#: Kinds this server can itself consume (and therefore locally account for).
LOCAL_KINDS = ("posts", "replies", "deletes")

#: Meta's documented per-24h totals, used only when the API omits ``quota_total``
#: or when we are falling back to the local log. Verified 2026-08-12.
DOCUMENTED_TOTALS: dict[str, int] = {
    "posts": 250,
    "replies": 1000,
    "deletes": 100,
    "location_searches": 500,
}

DEFAULT_WINDOW_SECONDS = 86400


def fields_for(kinds: tuple[str, ...]) -> str:
    return ",".join(field for kind in kinds for field in FIELD_MAP[kind])


#: ``fields`` value for the request, in Meta's documented order.
PUBLISHING_LIMIT_FIELDS = fields_for(tuple(FIELD_MAP))

#: **Field sets are negotiated, widest first.** Verified live against
#: @cyb3r_pete on 2026-08-12: asking for all four pairs returns **HTTP 500 with
#: an empty body**, and so does any request including
#: ``delete_quota_usage``/``delete_config`` or the ``location_search_*`` pair.
#: Posts + replies returns 200. The delete pair almost certainly fails because
#: this app was never granted ``threads_delete``, and one unsupported field
#: poisons the whole response rather than being omitted from it.
#:
#: So the client tries the widest set, then narrows, and remembers what worked.
#: Any kind that never comes back is reported as ``None`` (unknown), never as
#: zero, and falls back to the local log.
FIELD_SET_CANDIDATES: tuple[str, ...] = (
    fields_for(("posts", "replies", "deletes", "location_searches")),
    fields_for(("posts", "replies", "deletes")),
    fields_for(("posts", "replies")),
)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def parse_publishing_limit(payload: dict) -> dict[str, dict | None]:
    """Turn the raw ``threads_publishing_limit`` body into four normalized kinds.

    Returns ``{kind: {"used", "quota_total", "quota_total_source", "remaining",
    "window_seconds", "source"} | None}``. A kind is ``None`` when Meta did not
    return its usage field at all — unknown, which is not the same as zero.

    Raises :class:`ThreadsAPIError` when there is no data row, so the caller can
    fall back rather than treat an empty envelope as "no usage".
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ThreadsAPIError(
            "threads_publishing_limit returned no data row.",
            details={"body_keys": sorted(payload) if isinstance(payload, dict) else None},
        )
    row = rows[0]

    out: dict[str, dict | None] = {}
    for kind, (usage_field, config_field) in FIELD_MAP.items():
        used = _as_int(row.get(usage_field))
        if used is None:
            out[kind] = None
            continue
        config = row.get(config_field)
        config = config if isinstance(config, dict) else {}
        total = _as_int(config.get("quota_total"))
        total_source = "api"
        if total is None:
            total = DOCUMENTED_TOTALS[kind]
            total_source = "documented_default"
        window = _as_int(config.get("quota_duration")) or DEFAULT_WINDOW_SECONDS
        out[kind] = {
            "used": used,
            "quota_total": total,
            "quota_total_source": total_source,
            "remaining": max(0, total - used),
            "window_seconds": window,
            "source": "api",
        }
    return out


def local_view(publish_log, kind: str, now: float | None = None) -> dict:
    """One kind's budget as seen by the local publish log. Undercounts."""
    total = DOCUMENTED_TOTALS[kind]
    used = publish_log.used(kind, now=now)
    return {
        "used": used,
        "quota_total": total,
        "quota_total_source": "documented_default",
        "remaining": max(0, total - used),
        "window_seconds": DEFAULT_WINDOW_SECONDS,
        "source": "local_log",
    }


LOCAL_WARNING = (
    "Meta's threads_publishing_limit endpoint could not be read, so these counts "
    "come from this server's local publish log. They UNDERCOUNT: the log only "
    "sees posts, replies and deletes made through this MCP server, not ones made "
    "from the Threads app or any other client."
)


class QuotaGate:
    """Budget authority: Meta's endpoint first, the local log only as fallback.

    Caches the API snapshot for :attr:`CACHE_TTL` seconds so a 20-segment chain
    does not spend 20 extra API calls checking a number that barely moves, and
    optimistically decrements the cache after each publish so the cached window
    stays conservative rather than stale-optimistic.
    """

    CACHE_TTL = 60.0

    def __init__(
        self,
        fetch: Callable[[], Awaitable[dict[str, dict | None]]],
        publish_log,
    ):
        self._fetch = fetch
        self._log = publish_log
        self._cached: dict[str, dict | None] | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    async def snapshot(self, *, force: bool = False, now: float | None = None) -> dict:
        """Return the full budget picture, labelled with where it came from.

        ``{"source": "api"|"local_log"|"mixed", "authoritative": bool,
        "warning": str|None, "fetched_at": float|None, "quotas": {kind: {...}}}``
        """
        now = now if now is not None else time.time()
        api: dict[str, dict | None] | None = None
        error: str | None = None

        if not force and self._cached is not None and (now - self._cached_at) < self.CACHE_TTL:
            api = self._cached
        else:
            async with self._lock:
                # Double-check: another task may have refreshed while we waited.
                if (
                    not force
                    and self._cached is not None
                    and (time.time() - self._cached_at) < self.CACHE_TTL
                ):
                    api = self._cached
                else:
                    try:
                        api = await self._fetch()
                    except Exception as exc:  # the fallback IS the point
                        error = str(exc)
                        log.warning(
                            "threads_publishing_limit unavailable (%s); falling back "
                            "to the local publish log, which undercounts",
                            type(exc).__name__,
                        )
                    else:
                        self._cached = api
                        self._cached_at = time.time()

        quotas: dict[str, dict | None] = {}
        for kind in FIELD_MAP:
            from_api = (api or {}).get(kind)
            if from_api is not None:
                quotas[kind] = dict(from_api)
            elif kind in LOCAL_KINDS:
                quotas[kind] = local_view(self._log, kind, now)
            else:
                quotas[kind] = None

        sources = {q["source"] for q in quotas.values() if q}
        if sources == {"api"}:
            source = "api"
        elif sources == {"local_log"}:
            source = "local_log"
        else:
            source = "mixed"

        degraded = sorted(k for k, q in quotas.items() if q and q["source"] == "local_log")
        if source == "local_log":
            warning = LOCAL_WARNING
        elif source == "mixed":
            warning = (
                f"Meta did not report {', '.join(degraded)}, so "
                f"{'that quota is' if len(degraded) == 1 else 'those quotas are'} "
                "the local fallback and undercount; the rest are Meta's own counts."
            )
        else:
            warning = None
        return {
            "source": source,
            "authoritative": source == "api",
            "degraded_kinds": degraded,
            "warning": warning,
            "error": error,
            "fetched_at": self._cached_at or None,
            "quotas": quotas,
        }

    async def require(self, *, now: float | None = None, **needed: int) -> dict:
        """Refuse the operation unless every requested kind has headroom.

        ``require(posts=1, replies=19)``. Raises
        :class:`ThreadsRateLimitError` naming every quota that falls short and
        which source the decision came from. Returns the snapshot it used.
        """
        wanted = {k: int(v) for k, v in needed.items() if v}
        for kind in wanted:
            if kind not in FIELD_MAP:
                raise ValueError(f"unknown quota kind: {kind}")
        snap = await self.snapshot(now=now)
        if not wanted:
            return snap

        shortfalls = []
        for kind, count in wanted.items():
            view = snap["quotas"].get(kind)
            if view is None:
                # Unknown headroom: do not invent a refusal, but say so.
                log.warning("quota kind %s is unknown; proceeding without a check", kind)
                continue
            if view["remaining"] < count:
                shortfalls.append(
                    f"{kind}: need {count}, {view['remaining']} left of "
                    f"{view['quota_total']} per {view['window_seconds'] // 3600}h"
                )
        if shortfalls:
            raise ThreadsRateLimitError(
                "Threads publishing quota would be exceeded (" + "; ".join(shortfalls) + ").",
                details={
                    "needed": wanted,
                    "quotas": snap["quotas"],
                    "budget_source": snap["source"],
                    "authoritative": snap["authoritative"],
                    "warning": snap["warning"],
                },
            )
        return snap

    def consume(self, kind: str, count: int = 1, now: float | None = None) -> None:
        """Record ``count`` uses of ``kind`` locally and bump the cached snapshot.

        The local log is the fallback source, so it must stay accurate even
        while the API is the authority. Bumping the cache keeps a burst inside
        one TTL window from over-spending against a stale number.
        """
        if kind in LOCAL_KINDS:
            self._log.record(kind, count, now=now)
        cached = (self._cached or {}).get(kind)
        if cached is not None:
            cached["used"] += count
            cached["remaining"] = max(0, cached["remaining"] - count)
