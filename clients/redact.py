"""Secret redaction for logs.

The Threads token endpoints pass the access token as a **query parameter**
(``?access_token=...``), and httpx logs the full request URL at INFO level.
Left alone, that writes a live 60-day credential into container logs on every
refresh. This module is the deliberate mitigation:

1. :func:`redact_secrets` scrubs known secret-bearing query params and bearer
   headers out of an arbitrary string.
2. :class:`SecretRedactingFilter` applies it to every log record's message and
   args.
3. :func:`install_log_redaction` attaches the filter to the root logger and to
   the HTTP-client loggers, and drops ``httpx``/``httpcore`` to WARNING so the
   request-URL line never fires in the first place.

Belt and braces on purpose: the level change stops the known logger, the
filter catches anything else that ever formats a URL into a message.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

# Query params whose values are credentials.
_SECRET_PARAMS = (
    "access_token",
    "client_secret",
    "app_secret",
    "code",
    "refresh_token",
)

_PARAM_RE = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_PARAMS) + r")=([^&\s\"'\\]+)",
)

# `Authorization: Bearer xyz` / `"Authorization": "Bearer xyz"`.
# The 16-char floor keeps ordinary prose ("no bearer token configured") out of
# the match while still catching any real credential.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]{16,}=*)")

# Threads long-lived tokens are long opaque strings; catch bare ones that got
# formatted into a message without a surrounding param name.
_THREADS_TOKEN_RE = re.compile(r"\bTHQ[A-Za-z0-9_\-]{20,}")


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential values replaced by ``[REDACTED]``.

    Handles ``?access_token=...`` style query params, ``Bearer <token>``
    headers, and bare ``THQ...`` Threads tokens.

    Example::

        >>> redact_secrets("GET https://graph.threads.com/refresh_access_token"
        ...                "?grant_type=th_refresh_token&access_token=THQabc123")
        'GET https://graph.threads.com/refresh_access_token?grant_type=th_refresh_token&access_token=[REDACTED]'
    """
    if not text:
        return text
    out = _PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _THREADS_TOKEN_RE.sub(REDACTED, out)
    return out


class SecretRedactingFilter(logging.Filter):
    """Log filter that scrubs secrets from the record message and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (redact_secrets(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a for a in record.args
                )
        return True


_HTTP_LOGGERS = ("httpx", "httpx._client", "httpcore", "httpcore.http11", "hpack")


def install_log_redaction() -> SecretRedactingFilter:
    """Silence HTTP-client URL logging and attach the redaction filter.

    Returns the installed filter so tests (and callers that build their own
    handlers) can reuse the same instance.
    """
    flt = SecretRedactingFilter()

    root = logging.getLogger()
    if not any(isinstance(f, SecretRedactingFilter) for f in root.filters):
        root.addFilter(flt)
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(flt)

    for name in _HTTP_LOGGERS:
        lg = logging.getLogger(name)
        # httpx logs "HTTP Request: GET <full url with query>" at INFO.
        lg.setLevel(logging.WARNING)
        if not any(isinstance(f, SecretRedactingFilter) for f in lg.filters):
            lg.addFilter(flt)

    return flt
