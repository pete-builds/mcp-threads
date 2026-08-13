"""Exception hierarchy, split out so parsing modules can raise without importing
:mod:`clients.threads` (which imports them back).

Every class carries a ``code`` from the Standard Error Contract's fixed enum:
``UPSTREAM_DOWN``, ``AUTH_FAILED``, ``INVALID_INPUT``, ``NOT_FOUND``,
``RATE_LIMITED``, ``INTERNAL``. Adding a new class means picking one of those,
never inventing a sixth.
"""

from __future__ import annotations


class ThreadsError(Exception):
    """Base error. Carries a Standard Error Contract ``code``."""

    code = "INTERNAL"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class ThreadsAuthError(ThreadsError):
    """Credential is missing, dead, or rejected. Needs human re-auth."""

    code = "AUTH_FAILED"


class ThreadsScopeError(ThreadsAuthError):
    """The token is valid but was never granted the scope this call needs.

    Distinct from :class:`ThreadsAuthError` because the remedy is different: the
    credential is healthy, the *authorization* is too narrow, and scopes are
    bound at authorization time so no refresh can widen them. Shares the
    ``AUTH_FAILED`` code so the wire contract stays a fixed enum.
    """


class ThreadsInputError(ThreadsError):
    """Caller-side validation failure. Never reached the API."""

    code = "INVALID_INPUT"


class ThreadsRateLimitError(ThreadsError):
    """Upstream or local rate/quota limit."""

    code = "RATE_LIMITED"


class ThreadsAPIError(ThreadsError):
    """Upstream returned an error response."""

    code = "UPSTREAM_DOWN"
