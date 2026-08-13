"""Token-expiry health, shaped for an HTTP poller (Uptime Kuma).

**Why this exists.** ``token_status`` is an MCP tool, and Uptime Kuma cannot
call an MCP tool — it polls HTTP and reads a status code. This module computes
the same credential health as a plain dict plus an HTTP status code, so
``tools/health.py`` can hang it off a Starlette route.

Three properties are load-bearing:

1. **No network call.** Expiry is read from the persisted token store only.
   The endpoint is polled every 60s; it must never spend an API call, never
   trigger a refresh, and never consume the ``.env`` seed as a side effect
   (``ThreadsClient.load_state`` writes; :meth:`TokenStore.load` does not).
2. **No secrets.** Every field is an integer, an ISO timestamp, or a value
   from a closed vocabulary. Nothing read out of the token file is echoed
   verbatim — ``source`` is clamped to the known values and the free-text
   ``detail`` is fixed prose passed through :func:`redact_secrets` anyway.
3. **Never raises.** An empty volume, a truncated file, or binary garbage is a
   503 with a status word, not a stack trace out of an ASGI handler.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime

from clients.redact import redact_secrets
from clients.tokenstore import TokenStore

log = logging.getLogger("mcp-threads.health")

#: At or below this many whole days to expiry, the endpoint reports unhealthy.
#: Proactive refresh runs at day 45 (15 days remaining), so reaching 14 means
#: refresh has not been running and a human needs to look.
WARNING_THRESHOLD_DAYS = 14

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_CRITICAL = "critical"
STATUS_UNSEEDED = "unseeded"

#: The vocabulary ``TokenState.source`` uses. Anything else reads as unknown
#: rather than being echoed into the response body.
KNOWN_SOURCES = ("seed", "refresh")

HTTP_OK = 200
HTTP_UNAVAILABLE = 503


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _payload(
    *,
    status: str,
    detail: str,
    version: str,
    days_remaining: int | None = None,
    token_source: str | None = None,
    expires_at: str | None = None,
    refresh_count: int | None = None,
) -> dict:
    return {
        "status": status,
        "days_remaining": days_remaining,
        "token_source": token_source,
        "detail": redact_secrets(detail),
        "expires_at": expires_at,
        "refresh_count": refresh_count,
        "version": version,
    }


def health_snapshot(
    store: TokenStore, *, version: str, now: float | None = None
) -> tuple[dict, int]:
    """Return ``(body, http_status)`` describing Threads credential health.

    ``200`` only when a token exists and more than
    :data:`WARNING_THRESHOLD_DAYS` whole days remain. Everything else is
    ``503``: an unseeded volume is not a healthy server, so the monitor should
    say so rather than showing green.

    ``days_remaining`` is floored to whole days and the threshold is applied to
    that floored value, so body and status code never disagree. The cost is
    that alerting can fire up to a day early (14.9 days floors to 14). For a
    credential whose refresh window opens at 15 days remaining, erring early is
    the right direction.
    """
    now = time.time() if now is None else now

    try:
        state = store.load()
    except Exception as exc:  # a poller must never see a traceback
        log.error("health: token store unreadable: %s", type(exc).__name__)
        state = None

    if state is None:
        try:
            file_present = store.path.exists()
        except OSError:
            file_present = False
        if file_present:
            return (
                _payload(
                    status=STATUS_CRITICAL,
                    detail=(
                        "token store exists but is unreadable or corrupt; "
                        "re-run bootstrap.py and re-seed"
                    ),
                    version=version,
                ),
                HTTP_UNAVAILABLE,
            )
        return (
            _payload(
                status=STATUS_UNSEEDED,
                detail=("no token on the data volume; run bootstrap.py and set THREADS_SEED_TOKEN"),
                version=version,
            ),
            HTTP_UNAVAILABLE,
        )

    days = math.floor(state.seconds_remaining(now) / 86400.0)
    source = state.source if state.source in KNOWN_SOURCES else None
    common = {
        "days_remaining": days,
        "token_source": source,
        "expires_at": _iso(state.expires_at),
        "refresh_count": state.refresh_count,
        "version": version,
    }

    if state.is_expired(now):
        return (
            _payload(
                status=STATUS_CRITICAL,
                detail=(
                    "token has expired and cannot be refreshed; re-run bootstrap.py and re-seed"
                ),
                **common,
            ),
            HTTP_UNAVAILABLE,
        )

    if days <= WARNING_THRESHOLD_DAYS:
        return (
            _payload(
                status=STATUS_WARNING,
                detail=(
                    f"token expires in {days} day(s); proactive refresh "
                    "(day 45) has not been running"
                ),
                **common,
            ),
            HTTP_UNAVAILABLE,
        )

    return (
        _payload(status=STATUS_OK, detail="token healthy", **common),
        HTTP_OK,
    )
