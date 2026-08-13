"""Standard Error Contract helpers shared by every tool module."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pete_mcp_core import format_response

from clients.threads import ThreadsError

log = logging.getLogger("mcp-threads.tools")


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
