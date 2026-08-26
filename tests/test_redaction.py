"""Secrets must never reach a log record.

The Threads token endpoints pass the credential as a QUERY PARAM and httpx
logs full request URLs at INFO, so URL logging is a real leak vector here, not
a theoretical one.
"""

from __future__ import annotations

import json
import logging

import httpx
import respx

from clients.redact import (
    SecretRedactingFilter,
    install_log_redaction,
    redact_secrets,
)
from clients.threads import ThreadsClient
from clients.tokenstore import LONG_LIVED_TTL_SECONDS, TokenStore
from tests.test_client import AUTH, GRAPH, SEED, make_client, seed_volume

TOKEN = "THQVJabc123DEFghi456JKLmno789"


def test_access_token_query_param_is_redacted():
    url = f"https://graph.threads.com/refresh_access_token?grant_type=th_refresh_token&access_token={TOKEN}"
    out = redact_secrets(url)
    assert TOKEN not in out
    assert "access_token=[REDACTED]" in out
    assert "grant_type=th_refresh_token" in out  # non-secret params survive


def test_client_secret_query_param_is_redacted():
    out = redact_secrets("?grant_type=th_exchange_token&client_secret=abc123&x=1")
    assert "abc123" not in out
    assert "x=1" in out


def test_bearer_header_is_redacted():
    out = redact_secrets(f"Authorization: Bearer {TOKEN}")
    assert TOKEN not in out
    assert "[REDACTED]" in out


def test_bare_threads_token_is_redacted():
    out = redact_secrets(f"using token {TOKEN} now")
    assert TOKEN not in out


def test_non_secret_text_is_untouched():
    msg = "published media-123 to https://threads.net/p/abc"
    assert redact_secrets(msg) == msg


def test_positive_control_the_filter_actually_has_something_to_catch():
    """Guard against a silent no-op: prove the raw string WOULD leak."""
    raw = f"GET https://graph.threads.com/refresh_access_token?access_token={TOKEN}"
    assert TOKEN in raw  # unredacted really does contain the secret
    assert TOKEN not in redact_secrets(raw)


def test_log_filter_scrubs_the_record_message(caplog):
    logger = logging.getLogger("test.redaction.filter")
    logger.addFilter(SecretRedactingFilter())
    with caplog.at_level(logging.INFO, logger="test.redaction.filter"):
        logger.info("HTTP Request: GET %s", f"{AUTH}/refresh_access_token?access_token={TOKEN}")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in text
    assert "[REDACTED]" in text


def test_install_log_redaction_silences_httpx_url_logging():
    install_log_redaction()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert any(
        isinstance(f, SecretRedactingFilter)
        for f in logging.getLogger("httpx").filters
    )


@respx.mock
async def test_a_real_refresh_leaks_nothing_into_the_logs(tmp_path, caplog):
    """End-to-end: run a refresh with logging wide open, scan every record."""
    new_token = "THQbrandnew999888777666"
    respx.get(f"{AUTH}/refresh_access_token").mock(
        return_value=httpx.Response(
            200, json={"access_token": new_token, "expires_in": LONG_LIVED_TTL_SECONDS}
        )
    )
    respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )
    install_log_redaction()
    seed_volume(tmp_path, SEED, age_days=50)
    client = make_client(tmp_path, seed=None)

    with caplog.at_level(logging.DEBUG):
        await client.ensure_token()
        await client.get_profile()

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert new_token not in blob, "the refreshed token leaked into a log record"
    assert SEED not in blob, "the old token leaked into a log record"
    assert "super-secret" not in blob, "the app secret leaked into a log record"
    # And it really did refresh — otherwise this test proves nothing.
    assert TokenStore(tmp_path / "token.json").load().access_token == new_token
    await client.close()


def test_bearer_redaction_does_not_mangle_ordinary_prose():
    """Regression: 'no bearer token configured' must stay readable."""
    msg = "No bearer token configured; HTTP transport will accept unauthenticated requests"
    assert redact_secrets(msg) == msg


# --- the error path that reaches the agent, not just the log --------------
#
# This client sends the access token as a QUERY PARAMETER. The module docstring
# says so, and clients/redact.py exists precisely because of it. Redaction was
# installed on the LOG path and never on the path that reaches the agent, which
# is the narrower and more exposed of the two: a tool result goes straight into
# an agent's context.

TOKEN = "THQmaB1sIsAnExampleLongLivedThreadsToken0123456789"


def _error_for(body: str, content_type: str = "application/json"):
    resp = httpx.Response(
        400,
        content=body.encode(),
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://graph.threads.net/v1.0/me/threads"),
    )
    return ThreadsClient._error_from_response(resp, "GET", "/me/threads")


def test_an_upstream_message_quoting_the_request_is_redacted():
    """Meta's own `message` is documented, but it is still upstream text."""
    body = json.dumps({
        "error": {
            "message": (
                "Invalid OAuth access token for request "
                f"/v1.0/me/threads?access_token={TOKEN}"
            ),
            "code": 190,
        }
    })
    err = _error_for(body)
    rendered = json.dumps(err.details) + str(err)

    assert TOKEN not in rendered
    assert "[REDACTED]" in rendered
    # Still diagnosable.
    assert err.details["status"] == 400
    assert err.details["upstream_code"] == 190


def test_a_non_json_edge_page_is_not_forwarded_at_all():
    """A CDN or maintenance page can contain the request line, token and all.

    The old fallback pasted the first 300 characters of it verbatim.
    """
    html = (
        "<html><body>Request blocked: "
        f"GET /v1.0/me/threads?access_token={TOKEN}"
        "</body></html>"
    )
    err = _error_for(html, content_type="text/html")
    rendered = json.dumps(err.details) + str(err)

    assert TOKEN not in rendered
    assert "<html>" not in rendered
    # Enough to tell "Meta rejected this" from "something in front of Meta did".
    assert "non-JSON" in err.details["upstream_message"]
    assert "text/html" in err.details["upstream_message"]


def test_a_bare_threads_token_in_a_message_is_caught():
    """redact_secrets already knows every shape this repo's credentials take."""
    body = json.dumps({"error": {"message": f"token {TOKEN} is expired", "code": 190}})
    err = _error_for(body)

    assert TOKEN not in json.dumps(err.details)


def test_an_upstream_message_is_bounded():
    body = json.dumps({"error": {"message": "z" * 5000, "code": 1}})
    assert len(_error_for(body).details["upstream_message"]) <= 300


def test_an_empty_body_still_says_something():
    err = _error_for("", content_type="text/plain")
    assert err.details["upstream_message"] == "unknown error"
    assert err.details["status"] == 400
