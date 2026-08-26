"""Async Threads API client with volume-backed, self-refreshing credentials.

Token model (verified against Meta's docs 2026-08-12):

* Short-lived user token: 1 hour.
* Long-lived token: 60 days.
* Exchange: ``GET {auth}/access_token?grant_type=th_exchange_token&client_secret=..&access_token=..``
* Refresh:  ``GET {auth}/refresh_access_token?grant_type=th_refresh_token&access_token=..``
* Refresh allowed only when the token is >= 24h old and unexpired.
* Refresh returns a **new** access token that replaces the old one.
* Not refreshed within 60 days => permanently dead, manual re-auth required.

Two base hosts, both configurable, because Meta's own docs are inconsistent:
token endpoints are documented on ``graph.threads.com`` while publishing and
read endpoints are documented on ``graph.threads.net/v1.0``. Meta has been
migrating ``.net`` to ``.com``; do not hardcode one and assume.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from clients.errors import (
    ThreadsAPIError,
    ThreadsAuthError,
    ThreadsError,
    ThreadsInputError,
    ThreadsRateLimitError,
    ThreadsScopeError,
)
from clients.insights import (
    REPOST_FACADE_NOTE,
    parse_insights,
    summarize,
    validate_account_insights,
    validate_media_insights,
)
from clients.media import validate_alt_text, validate_image_url
from clients.quota import FIELD_SET_CANDIDATES, QuotaGate, parse_publishing_limit
from clients.redact import redact_secrets
from clients.text import (
    THREADS_LINK_LIMIT,
    THREADS_TEXT_LIMIT,
    count_unique_links,
    threads_length,
)
from clients.tokenstore import (
    LONG_LIVED_TTL_SECONDS,
    PublishLog,
    TokenState,
    TokenStore,
)

__all__ = [
    "DEFAULT_AUTH_BASE",
    "DEFAULT_GRANTED_SCOPES",
    "DEFAULT_GRAPH_BASE",
    "ThreadsAPIError",
    "ThreadsAuthError",
    "ThreadsClient",
    "ThreadsError",
    "ThreadsInputError",
    "ThreadsRateLimitError",
    "ThreadsScopeError",
]

log = logging.getLogger("mcp-threads.client")

DEFAULT_GRAPH_BASE = "https://graph.threads.net/v1.0"
DEFAULT_AUTH_BASE = "https://graph.threads.com"

#: Scopes actually granted on the live @cyb3r_pete token, confirmed with
#: ``debug_token`` on 2026-08-12. ``threads_delete`` is deliberately absent: it
#: was requested and NOT granted, which is why ``delete_post`` fails before it
#: ever reaches the API instead of returning a raw 403 that reads like a bug.
#: Override with ``THREADS_GRANTED_SCOPES``; set it empty to disable the
#: pre-flight entirely and let the API be the judge.
DEFAULT_GRANTED_SCOPES = (
    "threads_basic",
    "threads_content_publish",
    "threads_read_replies",
    "threads_manage_replies",
    "threads_manage_insights",
)

#: How many container IDs to remember, so a publish knows whether the thing it
#: is publishing is a reply (separate quota) and whether it needs processing.
_CONTAINER_MEMORY = 256

#: What each container status means for the caller's next move. Meta recommends
#: polling roughly once a minute for no more than 5 minutes.
_STATUS_GUIDANCE = {
    "FINISHED": "Ready. Call publish_post with this creation_id.",
    "IN_PROGRESS": (
        "Still processing. Wait about 60 seconds and check again; give up after "
        "5 minutes."
    ),
    "ERROR": (
        "Processing failed. Read error_message (for example INVALID_ASPECT_RATIO, "
        "FAILED_DOWNLOADING_VIDEO, INVALID_BIT_RATE), fix the source image, and "
        "create a new container. This one cannot be published."
    ),
    "EXPIRED": (
        "The container was not published within 24 hours and no longer exists. "
        "Create a new one."
    ),
    "PUBLISHED": "Already published. Publishing again would be rejected.",
}

POST_FIELDS = (
    "id,media_product_type,media_type,text,permalink,timestamp,shortcode,"
    "is_quote_post,has_replies"
)
REPLY_FIELDS = (
    "id,text,username,permalink,timestamp,replied_to,is_reply,hide_status"
)
PROFILE_FIELDS = "id,username,name,threads_profile_picture_url,threads_biography"

VALID_REPLY_CONTROL = {
    "everyone",
    "accounts_you_follow",
    "mentioned_only",
    "parent_post_author_only",
    "followers_only",
}


def scope_remedy(scope: str, action: str, granted: tuple[str, ...] | None) -> ThreadsScopeError:
    """Build the one error message a missing scope should ever produce.

    Deliberately verbose: the failure is a permissions gap in Meta's dashboard,
    not a defect in this code, and the difference has to be unmistakable from
    the message alone.
    """
    return ThreadsScopeError(
        f"{action} cannot run: this access token was never granted the "
        f"'{scope}' scope. This is a Meta app permission gap, not a bug in "
        f"mcp-threads, and no token refresh can fix it (scopes are bound at "
        f"authorization time). To enable it: open the Meta App Dashboard for the "
        f"Threads app, go to the Use cases page, Customize the Threads API use "
        f"case, add the '{scope}' permission, then re-run bootstrap.py to mint a "
        f"NEW token that carries the scope and re-seed THREADS_SEED_TOKEN.",
        details={
            "missing_scope": scope,
            "granted_scopes": list(granted) if granted is not None else None,
            "remedy": (
                "Meta App Dashboard > Use cases > Access the Threads API > "
                f"Customize > add '{scope}', then re-run bootstrap.py and re-seed."
            ),
            "refresh_will_not_help": True,
        },
    )


class ThreadsClient:
    """Threads Graph API client.

    The credential is mutable state on a named volume, not config. See
    :mod:`clients.tokenstore` for why.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        data_dir: str | Path,
        seed_token: str | None = None,
        seed_expires_at: float | None = None,
        graph_base: str = DEFAULT_GRAPH_BASE,
        auth_base: str = DEFAULT_AUTH_BASE,
        granted_scopes: tuple[str, ...] | None = DEFAULT_GRANTED_SCOPES,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._app_id = app_id
        self._app_secret = app_secret
        self._graph_base = graph_base.rstrip("/")
        self._auth_base = auth_base.rstrip("/")
        self._granted_scopes = tuple(granted_scopes) if granted_scopes else None

        data_path = Path(data_dir)
        self.store = TokenStore(data_path / "token.json")
        self.publish_log = PublishLog(data_path / "publish_log.json")
        self.quota = QuotaGate(self.fetch_publishing_limit, self.publish_log)

        self._seed_token = seed_token or None
        self._seed_expires_at = seed_expires_at

        self._state: TokenState | None = None
        self._token_lock = asyncio.Lock()
        self._user_id: str | None = None
        self._username: str | None = None
        self._containers: dict[str, dict] = {}
        # Widest threads_publishing_limit field set this account accepts; learned
        # on the first call because Meta 500s rather than omitting bad fields.
        self._limit_fields: str | None = None

        self._client = http_client or httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"User-Agent": "mcp-threads/0.1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # token lifecycle
    # ------------------------------------------------------------------

    def load_state(self) -> TokenState | None:
        """Load persisted state, adopting the ``.env`` seed if the volume is empty.

        The seed is consumed exactly once: as soon as it is adopted it is
        written to the volume, and from then on the volume is authoritative.
        A later refresh replaces the volume copy; the stale ``.env`` value is
        never read again. This is the 60-day trap, closed.
        """
        state = self.store.load()
        if state is not None:
            self._state = state
            return state

        if not self._seed_token:
            return None

        now = time.time()
        expires_at = self._seed_expires_at or (now + LONG_LIVED_TTL_SECONDS)
        state = TokenState(
            access_token=self._seed_token,
            expires_at=expires_at,
            obtained_at=now,
            last_refresh_at=None,
            refresh_count=0,
            source="seed",
        )
        self.store.save(state)
        log.info(
            "adopted seed token from environment; volume is authoritative from now on "
            "(days_remaining=%.1f)",
            state.days_remaining(now),
        )
        self._state = state
        return state

    def log_startup_status(self) -> None:
        """Log days-to-expiry at boot. Mandated by the design spec."""
        state = self._state or self.load_state()
        if state is None:
            log.critical(
                "no Threads token found on the data volume and no seed in the "
                "environment. Run bootstrap.py locally and set THREADS_SEED_TOKEN."
            )
            return
        days = state.days_remaining()
        if days <= 0:
            log.critical(
                "Threads token EXPIRED %.1f days ago. It cannot be refreshed. "
                "Re-run bootstrap.py and re-seed.",
                -days,
            )
        elif days <= 14:
            log.warning(
                "Threads token expires in %.1f days (source=%s, refreshes=%d)",
                days,
                state.source,
                state.refresh_count,
            )
        else:
            log.info(
                "Threads token OK: %.1f days remaining (source=%s, refreshes=%d)",
                days,
                state.source,
                state.refresh_count,
            )

    async def _refresh_token(self, state: TokenState) -> TokenState:
        """Exchange the current long-lived token for a new one and persist it.

        The token is sent as a **query parameter**, which is why log redaction
        is installed at startup (see :mod:`clients.redact`).
        """
        resp = await self._client.get(
            f"{self._auth_base}/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": state.access_token,
            },
        )
        if resp.status_code != 200:
            raise ThreadsAuthError(
                f"Threads token refresh failed ({resp.status_code}). "
                "If the token is more than 60 days old it is permanently dead "
                "and bootstrap.py must be re-run.",
                details={"status": resp.status_code},
            )
        data = resp.json()
        new_token = data.get("access_token")
        if not new_token:
            raise ThreadsAuthError(
                "Threads refresh response contained no access_token.",
                details={"keys": sorted(data.keys())},
            )
        now = time.time()
        expires_in = int(data.get("expires_in", LONG_LIVED_TTL_SECONDS))
        new_state = TokenState(
            access_token=new_token,
            expires_at=now + expires_in,
            obtained_at=now,
            last_refresh_at=now,
            refresh_count=state.refresh_count + 1,
            source="refresh",
        )
        # Persist BEFORE swapping the in-memory copy: if the write fails we
        # keep using the old (still valid) token rather than holding a new one
        # that no restart would ever see.
        self.store.save(new_state)
        self._state = new_state
        log.info(
            "Threads token refreshed; %.1f days remaining (refresh #%d)",
            new_state.days_remaining(now),
            new_state.refresh_count,
        )
        return new_state

    async def ensure_token(self, force: bool = False) -> TokenState:
        """Return a usable token, refreshing proactively at day 45.

        Guarded by an ``asyncio.Lock`` with a double-check inside the lock so
        concurrent tool calls cannot both fire a refresh. That matters more
        here than for Spotify: each Threads refresh *replaces* the credential,
        so a double refresh would race two different tokens into the store.
        """
        state = self._state or self.load_state()
        if state is None:
            raise ThreadsAuthError(
                "No Threads credential available. The data volume is empty and "
                "THREADS_SEED_TOKEN is unset. Run bootstrap.py locally and seed it."
            )
        if not force and not state.needs_refresh():
            if state.is_expired():
                raise ThreadsAuthError(
                    "Threads token expired and can no longer be refreshed. "
                    "Re-run bootstrap.py and re-seed THREADS_SEED_TOKEN."
                )
            return state

        async with self._token_lock:
            # Double-check: another task may have refreshed while we waited.
            state = self._state or self.load_state()
            if state is None:
                raise ThreadsAuthError("No Threads credential available.")
            if state.is_expired():
                raise ThreadsAuthError(
                    "Threads token expired and can no longer be refreshed. "
                    "Re-run bootstrap.py and re-seed THREADS_SEED_TOKEN."
                )
            if not force and not state.needs_refresh():
                return state
            return await self._refresh_token(state)

    # ------------------------------------------------------------------
    # request plumbing
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        base: str | None = None,
    ) -> dict:
        """Issue a Graph request, forcing one token refresh on a 401 before failing."""
        state = await self.ensure_token()
        url = f"{(base or self._graph_base).rstrip('/')}{path}"

        for attempt in range(2):
            call_params = dict(params or {})
            call_params["access_token"] = state.access_token
            try:
                resp = await self._client.request(method, url, params=call_params)
            except httpx.ConnectError as exc:
                raise ThreadsAPIError(
                    "Threads API is unreachable.",
                    details={"host": url.split("/")[2], "exception": str(exc)},
                ) from exc
            except httpx.TimeoutException as exc:
                raise ThreadsAPIError(
                    "Threads API timed out.", details={"exception": str(exc)}
                ) from exc

            if resp.status_code == 401 and attempt == 0:
                log.warning("Threads API returned 401; forcing a token refresh and retrying")
                state = await self.ensure_token(force=True)
                continue

            if resp.status_code == 429:
                raise ThreadsRateLimitError(
                    "Threads API rate limit hit.",
                    details={
                        "status": 429,
                        "retry_after": resp.headers.get("Retry-After"),
                    },
                )

            if resp.status_code >= 400:
                raise self._error_from_response(resp, method, path)

            if not resp.content:
                return {}
            return resp.json()

        raise ThreadsAPIError(f"Threads request failed after retry: {method} {path}")

    @staticmethod
    def _error_from_response(
        resp: httpx.Response, method: str, path: str
    ) -> ThreadsError:
        """Map an upstream error body onto the Standard Error Contract."""
        body: dict = {}
        try:
            body = resp.json()
        except ValueError:
            body = {}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        upstream_code = err.get("error_user_title") or err.get("code")
        # Two changes here, and they close the same gap from opposite sides.
        #
        # The raw-body fallback is gone. This client sends the access token as
        # a QUERY PARAMETER -- the module docstring says so, and log redaction
        # exists precisely because of it -- so a body that is not the documented
        # JSON envelope can contain the request line, token and all. That is
        # every edge failure: a CDN error page, a redirect notice, a
        # maintenance interstitial. Reporting the body's shape instead is
        # enough to tell "Meta rejected this" from "something in front of Meta
        # answered", and forwards nothing.
        #
        # And Meta's own `message` is redacted before it goes anywhere. It is
        # the documented field, but it is still upstream text that can quote
        # the request back, and redact_secrets already knows every shape this
        # repo's credentials take. It was installed on the LOG path and never
        # on the path that reaches the agent, which is the narrower and more
        # exposed of the two.
        if err.get("message"):
            message = redact_secrets(str(err["message"]))[:300]
        elif resp.text:
            message = (
                f"non-JSON upstream response "
                f"({resp.headers.get('content-type', 'unknown')}, "
                f"{len(resp.text)} bytes)"
            )
        else:
            message = "unknown error"
        details = {
            "status": resp.status_code,
            "upstream_code": upstream_code,
            "upstream_subcode": err.get("error_subcode"),
            "upstream_message": message,
            "path": path,
        }
        if "LINK_LIMIT_EXCEEDED" in str(message).upper():
            return ThreadsInputError(
                f"Threads rejected the post: more than {THREADS_LINK_LIMIT} unique links.",
                details=details,
            )
        if resp.status_code in (400, 403, 404):
            code_cls = (
                ThreadsAuthError if resp.status_code == 403 else ThreadsInputError
            )
            return code_cls(
                f"Threads API rejected {method} {path}: {message}", details=details
            )
        return ThreadsAPIError(
            f"Threads API error {resp.status_code} on {method} {path}: {message}",
            details=details,
        )

    # ------------------------------------------------------------------
    # profile
    # ------------------------------------------------------------------

    async def get_profile(self) -> dict:
        """Return the authenticated Threads profile. Caches id/username."""
        data = await self._request("GET", "/me", params={"fields": PROFILE_FIELDS})
        self._user_id = data.get("id") or self._user_id
        self._username = data.get("username") or self._username
        return data

    async def get_user_id(self) -> str:
        """Resolve and cache the numeric Threads user ID."""
        if self._user_id:
            return self._user_id
        await self.get_profile()
        if not self._user_id:
            raise ThreadsAPIError("Threads /me returned no user id.")
        return self._user_id

    async def token_status(self) -> dict:
        """Credential health. Never returns the token itself."""
        state = self._state or self.load_state()
        if state is None:
            return {
                "valid": False,
                "reason": "no credential on the data volume and no seed in the environment",
                "days_remaining": None,
                "store_path": str(self.store.path),
            }
        now = time.time()
        return {
            "valid": not state.is_expired(now),
            "days_remaining": round(state.days_remaining(now), 2),
            "expires_at": _iso(state.expires_at),
            "obtained_at": _iso(state.obtained_at),
            "last_refresh_at": _iso(state.last_refresh_at),
            "refresh_count": state.refresh_count,
            "refresh_due_at": _iso(state.refresh_due_at()),
            "refresh_due_in_days": round((state.refresh_due_at() - now) / 86400.0, 2),
            "source": state.source,
            "store_path": str(self.store.path),
            "granted_scopes": list(self._granted_scopes) if self._granted_scopes else None,
            # Local-log counts only, kept here because this tool is deliberately
            # network-free. Call get_publishing_limit for Meta's real numbers.
            "publishes_used_24h": self.publish_log.used("posts", now=now),
            "publishes_remaining_24h": self.publish_log.remaining("posts", now=now),
            "replies_used_24h_local": self.publish_log.used("replies", now=now),
            "deletes_used_24h_local": self.publish_log.used("deletes", now=now),
            "budget_source": "local_log",
            "budget_note": (
                "These counts come from this server's local log and undercount "
                "anything posted from the Threads app. Call get_publishing_limit "
                "for Meta's authoritative post/reply/delete quotas."
            ),
        }

    # ------------------------------------------------------------------
    # scopes
    # ------------------------------------------------------------------

    def require_scope(self, scope: str, *, action: str) -> None:
        """Fail before the call when a needed scope was never granted.

        No-op when the granted set is unknown (``THREADS_GRANTED_SCOPES`` empty):
        an unverified guess must never block a call the API would have allowed.
        """
        if self._granted_scopes is None or scope in self._granted_scopes:
            return
        raise scope_remedy(scope, action, self._granted_scopes)

    # ------------------------------------------------------------------
    # quota
    # ------------------------------------------------------------------

    async def fetch_publishing_limit(self) -> dict:
        """Meta's authoritative quotas, negotiating the widest field set that works.

        Live on @cyb3r_pete, asking for all four usage/config pairs returns HTTP
        500 with an empty body: the ``delete_*`` and ``location_search_*`` pairs
        are not answerable for this app, and one unsupported field fails the
        whole response instead of being dropped from it. So try widest first,
        narrow on failure, and remember the set that worked.
        """
        user_id = await self.get_user_id()
        attempts = [self._limit_fields] if self._limit_fields else []
        attempts += [f for f in FIELD_SET_CANDIDATES if f != self._limit_fields]

        last_error: ThreadsError | None = None
        for fields in attempts:
            try:
                data = await self._request(
                    "GET",
                    f"/{user_id}/threads_publishing_limit",
                    params={"fields": fields},
                )
                parsed = parse_publishing_limit(data)
            except ThreadsError as exc:
                last_error = exc
                log.info(
                    "threads_publishing_limit rejected a %d-field request; narrowing",
                    len(fields.split(",")),
                )
                continue
            if self._limit_fields != fields:
                log.info(
                    "threads_publishing_limit negotiated to %d fields", len(fields.split(","))
                )
                self._limit_fields = fields
            return parsed
        self._limit_fields = None
        raise last_error or ThreadsAPIError("threads_publishing_limit returned nothing usable.")

    async def publishing_limit(self, force: bool = False) -> dict:
        """Full budget snapshot, labelled with the source it came from."""
        snapshot = await self.quota.snapshot(force=force)
        snapshot["fields_negotiated"] = self._limit_fields
        return snapshot

    # ------------------------------------------------------------------
    # insights
    # ------------------------------------------------------------------

    async def media_insights(self, media_id: str, metrics=None) -> dict:
        """Insights for one post. Empty for REPOST_FACADE media."""
        self.require_scope("threads_manage_insights", action="get_post_insights")
        if not media_id or not str(media_id).strip():
            raise ThreadsInputError("media_id is required.")
        request = validate_media_insights(metrics)
        data = await self._request(
            "GET", f"/{str(media_id).strip()}/insights", params=request["params"]
        )
        parsed = parse_insights(data)
        out = {
            "media_id": str(media_id).strip(),
            "requested_metrics": request["metrics"],
            "metrics": parsed,
            "summary": summarize(parsed),
        }
        if not parsed:
            out["note"] = REPOST_FACADE_NOTE
        return out

    async def account_insights(
        self, metrics=None, since=None, until=None, breakdown: str | None = None
    ) -> dict:
        """Insights for the authenticated profile."""
        self.require_scope("threads_manage_insights", action="get_account_insights")
        request = validate_account_insights(metrics, since, until, breakdown)
        user_id = await self.get_user_id()
        data = await self._request(
            "GET", f"/{user_id}/threads_insights", params=request["params"]
        )
        parsed = parse_insights(data)
        out = {
            "user_id": user_id,
            "requested_metrics": request["metrics"],
            "range": {
                "since": request["params"].get("since"),
                "until": request["params"].get("until"),
                "note": (
                    "Omitting since/until makes Threads default to a 2-day window."
                    if "since" not in request["params"] and "until" not in request["params"]
                    else None
                ),
            },
            "breakdown": breakdown,
            "metrics": parsed,
            "summary": summarize(parsed),
        }
        if not parsed:
            out["note"] = "Threads returned no insight rows for this request."
        return out

    # ------------------------------------------------------------------
    # publishing
    # ------------------------------------------------------------------

    def validate_text(
        self, text: str, link_attachment: str | None = None
    ) -> dict:
        """Client-side validation before spending an API call.

        Raises :class:`ThreadsInputError` on over-length text or more than five
        unique links (which the API would reject as
        ``THREADS_API__LINK_LIMIT_EXCEEDED``).
        """
        if not text or not text.strip():
            raise ThreadsInputError("Post text is empty.")
        length = threads_length(text)
        if length > THREADS_TEXT_LIMIT:
            raise ThreadsInputError(
                f"Post is {length} of a maximum {THREADS_TEXT_LIMIT} "
                "(Threads counts emoji as UTF-8 bytes). Use post_chain for long text.",
                details={"length": length, "limit": THREADS_TEXT_LIMIT},
            )
        links = count_unique_links(text, link_attachment)
        if links > THREADS_LINK_LIMIT:
            raise ThreadsInputError(
                f"Post has {links} unique links; Threads allows {THREADS_LINK_LIMIT}.",
                details={"links": links, "limit": THREADS_LINK_LIMIT},
            )
        return {"length": length, "link_count": links}

    async def create_container(
        self,
        text: str | None = None,
        *,
        image_url: str | None = None,
        alt_text: str | None = None,
        reply_to_id: str | None = None,
        link_attachment: str | None = None,
        topic_tag: str | None = None,
        reply_control: str | None = None,
        quote_post_id: str | None = None,
    ) -> dict:
        """Step 1 of the two-step publish: create an inert media container.

        With ``image_url`` this creates an ``IMAGE`` container (text optional);
        without it, a ``TEXT`` container (text required). Nothing appears on
        the timeline until :meth:`publish_container` runs.

        Returns ``{"creation_id", "media_type", "is_reply",
        "requires_status_check", "length", "link_count"}``.
        """
        if image_url is not None:
            image_url = validate_image_url(image_url)
            alt_text = validate_alt_text(alt_text)
        elif alt_text is not None:
            raise ThreadsInputError("alt_text only applies to an image post.")

        if text is None or not str(text).strip():
            if image_url is None:
                raise ThreadsInputError("Post text is empty.")
            meta = {"length": 0, "link_count": 0}
            text = None
        else:
            meta = self.validate_text(text, link_attachment)

        if topic_tag is not None:
            tag = topic_tag.strip()
            if not 1 <= len(tag) <= 50:
                raise ThreadsInputError("topic_tag must be 1 to 50 characters.")
            if "." in tag or "&" in tag:
                raise ThreadsInputError("topic_tag cannot contain a period or ampersand.")
            topic_tag = tag

        if reply_control is not None and reply_control not in VALID_REPLY_CONTROL:
            raise ThreadsInputError(
                f"reply_control must be one of {sorted(VALID_REPLY_CONTROL)}.",
                details={"got": reply_control},
            )

        user_id = await self.get_user_id()
        media_type = "IMAGE" if image_url else "TEXT"
        params: dict = {"media_type": media_type}
        if text:
            params["text"] = text
        if image_url:
            params["image_url"] = image_url
        if alt_text:
            params["alt_text"] = alt_text
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        if link_attachment:
            params["link_attachment"] = link_attachment
        if topic_tag:
            params["topic_tag"] = topic_tag
        if reply_control:
            params["reply_control"] = reply_control
        if quote_post_id:
            params["quote_post_id"] = quote_post_id

        # Deliberately NOT using auto_publish_text. The two-step separation is
        # a safety boundary: an agent that misfires creates an inert container
        # instead of a live post.
        data = await self._request("POST", f"/{user_id}/threads", params=params)
        creation_id = data.get("id")
        if not creation_id:
            raise ThreadsAPIError(
                "Threads container creation returned no id.", details={"body": data}
            )
        result = {
            "creation_id": creation_id,
            "media_type": media_type,
            "is_reply": bool(reply_to_id),
            # A TEXT container is publishable immediately; an IMAGE container
            # has to be downloaded and processed by Meta first.
            "requires_status_check": media_type != "TEXT",
            "quota_consumed_on_publish": "replies" if reply_to_id else "posts",
            "length": meta["length"],
            "link_count": meta["link_count"],
        }
        self._remember_container(creation_id, result)
        return result

    def _remember_container(self, creation_id: str, info: dict) -> None:
        """Bounded FIFO memory so publish knows which quota a container spends."""
        if len(self._containers) >= _CONTAINER_MEMORY:
            self._containers.pop(next(iter(self._containers)), None)
        self._containers[creation_id] = {
            "is_reply": info["is_reply"],
            "media_type": info["media_type"],
        }

    async def get_container_status(self, container_id: str) -> dict:
        """Check whether a container is eligible to publish.

        Statuses: ``EXPIRED`` (unpublished for 24h, gone), ``ERROR`` (see
        ``error_message``), ``FINISHED`` (ready to publish), ``IN_PROGRESS``
        (still processing), ``PUBLISHED``.
        """
        data = await self._request(
            "GET",
            f"/{container_id}",
            params={"fields": "status,error_message,id"},
        )
        status = data.get("status")
        remembered = self._containers.get(container_id, {})
        return {
            "container_id": data.get("id") or container_id,
            "status": status,
            "error_message": data.get("error_message"),
            "ready_to_publish": status == "FINISHED",
            "media_type": remembered.get("media_type"),
            "guidance": _STATUS_GUIDANCE.get(
                status, "Unrecognised status; treat as not ready and re-check."
            ),
        }

    async def publish_container(self, creation_id: str, *, is_reply: bool | None = None) -> dict:
        """Step 2: commit a container to the timeline.

        Charges the correct quota: a container created with ``reply_to_id``
        spends the separate 1000-replies-per-24h budget, not the 250-posts one.
        Headroom is checked against Meta's authoritative endpoint, falling back
        to the local log only if that call fails.

        Returns ``{"media_id", "permalink", "quota_kind", "budget_source"}``.
        """
        if is_reply is None:
            is_reply = bool(self._containers.get(creation_id, {}).get("is_reply"))
        kind = "replies" if is_reply else "posts"
        snap = await self.quota.require(**{kind: 1})

        user_id = await self.get_user_id()
        try:
            data = await self._request(
                "POST", f"/{user_id}/threads_publish", params={"creation_id": creation_id}
            )
        except ThreadsError as exc:
            raise await self._explain_publish_failure(creation_id, exc) from exc
        media_id = data.get("id")
        if not media_id:
            raise ThreadsAPIError(
                "Threads publish returned no media id.", details={"body": data}
            )
        self.quota.consume(kind, 1)
        permalink = None
        try:
            detail = await self._request(
                "GET", f"/{media_id}", params={"fields": "id,permalink,timestamp"}
            )
            permalink = detail.get("permalink")
        except ThreadsError:
            # The post IS live; failing to fetch its permalink must not look
            # like a failed publish.
            log.warning("published %s but could not fetch its permalink", media_id)
        return {
            "media_id": media_id,
            "permalink": permalink,
            "quota_kind": kind,
            "budget_source": snap["source"],
        }

    async def _explain_publish_failure(
        self, creation_id: str, exc: ThreadsError
    ) -> ThreadsError:
        """Attach the container's own status to a failed publish, if we can get it.

        An image container that failed processing rejects the publish with a
        generic message; the useful string (``INVALID_ASPECT_RATIO``,
        ``FAILED_DOWNLOADING_VIDEO``, ...) only lives on the container. Best
        effort: a failure here must never replace the original error.
        """
        try:
            status = await self.get_container_status(creation_id)
        except Exception:  # diagnostics must never mask the real error
            return exc
        if status.get("status") in ("ERROR", "EXPIRED", "IN_PROGRESS"):
            exc.details = {
                **exc.details,
                "container_status": status.get("status"),
                "container_error_message": status.get("error_message"),
                "guidance": status.get("guidance"),
            }
        return exc

    async def delete_media(self, media_id: str) -> dict:
        """Delete a published post. Destructive and not idempotent-safe.

        Gated on the ``threads_delete`` scope *before* the call, because that
        scope is not granted on the live token and a raw 403 reads like a bug
        in this server rather than a missing permission.
        """
        self.require_scope("threads_delete", action="delete_post")
        await self.quota.require(deletes=1)
        try:
            await self._request("DELETE", f"/{media_id}")
        except ThreadsAuthError as exc:
            # Belt and braces: if the pre-flight was disabled or the granted set
            # is stale, translate the upstream 403 into the same guidance.
            if exc.details.get("status") == 403:
                raise scope_remedy(
                    "threads_delete", "delete_post", self._granted_scopes
                ) from exc
            raise
        self.quota.consume("deletes", 1)
        return {"deleted": True, "media_id": media_id}

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    async def list_posts(self, limit: int = 10) -> list[dict]:
        """Recent posts on the authenticated profile, newest first."""
        user_id = await self.get_user_id()
        data = await self._request(
            "GET",
            f"/{user_id}/threads",
            params={"fields": POST_FIELDS, "limit": max(1, min(limit, 100))},
        )
        return [_shape_post(p) for p in data.get("data", []) or []]

    async def get_replies(self, media_id: str, all_depths: bool = False) -> list[dict]:
        """Replies to a post. ``all_depths`` uses ``/conversation`` instead of
        ``/replies`` (top level only)."""
        endpoint = "conversation" if all_depths else "replies"
        data = await self._request(
            "GET", f"/{media_id}/{endpoint}", params={"fields": REPLY_FIELDS}
        )
        return [_shape_reply(r) for r in data.get("data", []) or []]


# ----------------------------------------------------------------------
# shaping helpers
# ----------------------------------------------------------------------


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _shape_post(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "text": p.get("text"),
        "media_type": p.get("media_type"),
        "permalink": p.get("permalink"),
        "timestamp": p.get("timestamp"),
        "shortcode": p.get("shortcode"),
        "has_replies": p.get("has_replies"),
    }


def _shape_reply(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "text": r.get("text"),
        "username": r.get("username"),
        "permalink": r.get("permalink"),
        "timestamp": r.get("timestamp"),
        "replied_to_id": (r.get("replied_to") or {}).get("id"),
        "hide_status": r.get("hide_status"),
    }
