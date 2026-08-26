"""Standard Error Contract helpers shared by every tool module."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pete_mcp_core import format_response

from clients.threads import ThreadsError

log = logging.getLogger("mcp-threads.tools")

# --- Tool annotations ---
# Nothing in an MCP manifest distinguishes delete_post from whoami unless the
# tool says so. Without these hints a client has no basis on which to prompt
# before a call, and on this server that gap is sharper than most: publish_post
# and post_chain write to a PUBLIC account under Pete's name, and delete_post
# removes something already published.
#
# openWorldHint is True everywhere except preview_chain, which is pure local
# segmentation and makes no network call at all.

#: Reads only. Safe to repeat, safe to call speculatively.
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: Reads only, and does not leave this process.
READ_ONLY_LOCAL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

#: Creates something. Calling twice creates two, which is the whole reason
#: this is separate from a plain write: a retried publish is a duplicate post
#: on a public timeline, not a no-op.
CREATE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}

#: Removes something already public. The call worth confirming.
DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}


def ok(data: Any) -> str:
    """Success envelope: ``{"data": ...}``."""
    return format_response({"data": data})


def fail(message: str, code: str, details: dict | None = None) -> str:
    """Failure envelope: ``{"error", "code", "details"}``."""
    payload: dict[str, Any] = {"error": message, "code": code}
    if details:
        payload["details"] = details
    return format_response(payload)


def tool_guard(
    func: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[str]]:
    """Turn any escaping exception into the Standard Error Contract.

    ``ThreadsError`` subclasses carry their own ``code`` and ``details``;
    anything else becomes ``INTERNAL``. No exception ever reaches Claude.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except ThreadsError as exc:
            log.error("tool %s failed (%s): %s", func.__name__, exc.code, exc)
            return fail(str(exc), exc.code, exc.details)
        except Exception as exc:
            log.error("tool %s raised %s: %s", func.__name__, type(exc).__name__, exc)
            return fail(
                f"Unexpected failure in {func.__name__}: {exc}",
                "INTERNAL",
                {"exception": type(exc).__name__},
            )

    return wrapper
