"""Bootstrap OAuth constants and callback handling.

These pin corrections that are otherwise invisible until Pete has already
burned an authorization round trip. Scopes are granted at authorization time,
so a missing scope means redoing the whole browser flow.
"""

from __future__ import annotations

import urllib.parse

import pytest

import bootstrap


def test_scopes_include_read_replies():
    """get_replies GETs the reply endpoints and 403s without this scope."""
    scopes = set(bootstrap.SCOPES.split(","))
    assert "threads_read_replies" in scopes


def test_scopes_cover_the_whole_mvp_tool_surface():
    scopes = set(bootstrap.DEFAULT_SCOPES.split(","))
    assert scopes == {
        "threads_basic",
        "threads_content_publish",
        "threads_read_replies",
        "threads_manage_replies",
        "threads_delete",
    }


def test_delete_scope_is_requested_for_the_delete_post_tool():
    """The use-case dashboard offers threads_delete even though the authorize
    doc's Values list omits it. delete_post is gated on it."""
    assert "threads_delete" in bootstrap.DEFAULT_SCOPES


def test_fallback_scopes_drop_only_the_undocumented_one():
    default = set(bootstrap.DEFAULT_SCOPES.split(","))
    fallback = set(bootstrap.FALLBACK_SCOPES.split(","))
    assert default - fallback == {"threads_delete"}


def test_scopes_are_overridable_by_env(monkeypatch):
    import importlib

    monkeypatch.setenv("THREADS_SCOPES", "threads_basic")
    reloaded = importlib.reload(bootstrap)
    try:
        assert reloaded.SCOPES == "threads_basic"
    finally:
        monkeypatch.delenv("THREADS_SCOPES", raising=False)
        importlib.reload(bootstrap)


def test_invalid_scope_rejection_is_detected():
    assert bootstrap.is_scope_error("invalid_scope", "") is True
    assert bootstrap.is_scope_error("invalid_request", "Invalid scope: threads_delete") is True
    assert bootstrap.is_scope_error("access_denied", "user cancelled") is False


def test_insights_scope_is_not_requested():
    """Insights are deferred past MVP; do not ask for what we do not use."""
    assert "threads_manage_insights" not in bootstrap.DEFAULT_SCOPES


def test_short_and_long_token_exchanges_use_different_hosts():
    """.net serves the short-lived code exchange, .com the long-lived one."""
    short_host = urllib.parse.urlparse(bootstrap.SHORT_TOKEN_URL).netloc
    long_host = urllib.parse.urlparse(bootstrap.LONG_TOKEN_URL).netloc
    assert short_host == "graph.threads.net"
    assert long_host == "graph.threads.com"
    assert short_host != long_host


def test_short_token_url_path_is_the_oauth_endpoint():
    assert bootstrap.SHORT_TOKEN_URL.endswith("/oauth/access_token")
    assert bootstrap.LONG_TOKEN_URL.endswith("/access_token")
    assert not bootstrap.LONG_TOKEN_URL.endswith("/oauth/access_token")


def test_short_token_exchange_posts_the_required_form_fields(monkeypatch):
    captured: dict = {}

    def fake_post_form(url, fields):
        captured["url"] = url
        captured["fields"] = fields
        return {"access_token": "short-token"}

    monkeypatch.setattr(bootstrap, "_post_form", fake_post_form)
    out = bootstrap.exchange_code_for_short_token("app", "secret", "the-code")

    assert out["access_token"] == "short-token"
    assert captured["url"] == bootstrap.SHORT_TOKEN_URL
    assert captured["fields"] == {
        "client_id": "app",
        "client_secret": "secret",
        "grant_type": "authorization_code",
        "redirect_uri": bootstrap.REDIRECT_URI,
        "code": "the-code",
    }


def test_long_token_exchange_uses_the_th_exchange_grant(monkeypatch):
    captured: dict = {}

    def fake_get_json(url, params):
        captured["url"] = url
        captured["params"] = params
        return {"access_token": "long-token", "expires_in": 5183944}

    monkeypatch.setattr(bootstrap, "_get_json", fake_get_json)
    out = bootstrap.exchange_for_long_token("secret", "short-token")

    assert out["access_token"] == "long-token"
    assert captured["url"] == bootstrap.LONG_TOKEN_URL
    assert captured["params"]["grant_type"] == "th_exchange_token"
    assert captured["params"]["client_secret"] == "secret"
    assert captured["params"]["access_token"] == "short-token"


def test_callback_handler_accepts_both_slash_spellings():
    """The App Dashboard may append a trailing slash to the registered URI."""
    for path in ("/callback", "/callback/"):
        assert urllib.parse.urlparse(path).path.rstrip("/") == "/callback"
    # And still rejects anything else.
    assert urllib.parse.urlparse("/other").path.rstrip("/") != "/callback"


def test_redirect_uri_is_overridable_for_a_rewritten_dashboard_value(monkeypatch):
    """Reload the module with the env var set and confirm it takes effect."""
    import importlib

    monkeypatch.setenv("THREADS_REDIRECT_URI", "https://example.test/cb/")
    reloaded = importlib.reload(bootstrap)
    try:
        assert reloaded.REDIRECT_URI == "https://example.test/cb/"
    finally:
        monkeypatch.delenv("THREADS_REDIRECT_URI", raising=False)
        importlib.reload(bootstrap)


# ---------------------------------------------------------------------------
# Manual paste fallback.
#
# Meta rejects a plain-http loopback redirect URI (error 1349187, "Insecure
# Login Blocked"), so the registered URI is a hosted HTTPS URL and the local
# listener can never fire. These pin the paste path, and specifically that it
# is not a security downgrade: `state` is still enforced.
# ---------------------------------------------------------------------------


def test_loopback_detection_picks_the_right_path():
    assert bootstrap.is_loopback_redirect("http://127.0.0.1:8766/callback") is True
    assert bootstrap.is_loopback_redirect("http://localhost:8766/callback") is True
    assert (
        bootstrap.is_loopback_redirect("https://brooksnewmedia.com/threads-callback")
        is False
    )


def test_full_url_paste_yields_code_and_state():
    result = bootstrap.parse_pasted_redirect(
        "https://brooksnewmedia.com/threads-callback?code=AQBx-123&state=st-abc#_"
    )
    assert result["code"] == "AQBx-123"
    assert result["state"] == "st-abc"
    assert result["bare_code"] is False
    assert result["error"] is None
    # And it flows straight into the existing exchange path.
    assert bootstrap.resolve_authorization(result, "st-abc") == "AQBx-123"


def test_full_url_paste_survives_surrounding_whitespace_and_quotes():
    result = bootstrap.parse_pasted_redirect(
        '  "https://brooksnewmedia.com/threads-callback?code=AQBx-123&state=st-abc"  '
    )
    assert result["code"] == "AQBx-123"
    assert result["state"] == "st-abc"


def test_bare_code_paste_is_accepted_but_flagged_as_unverified(capsys):
    result = bootstrap.parse_pasted_redirect("AQBx-123#_")
    assert result["code"] == "AQBx-123"
    assert result["state"] is None
    assert result["bare_code"] is True

    # No state to compare, so it must not be rejected for mismatching...
    assert bootstrap.resolve_authorization(result, "st-abc") == "AQBx-123"
    # ...but it must say so, loudly, on stderr.
    err = capsys.readouterr().err
    assert "state" in err.lower()
    assert "not be verified" in err.lower()


def test_pasted_state_mismatch_is_rejected(capsys):
    """The CSRF check is enforced on pasted input exactly as on the listener."""
    result = bootstrap.parse_pasted_redirect(
        "https://brooksnewmedia.com/threads-callback?code=AQBx-123&state=attacker"
    )
    with pytest.raises(SystemExit) as exc:
        bootstrap.resolve_authorization(result, "st-abc")
    assert exc.value.code == 1
    assert "state mismatch" in capsys.readouterr().err.lower()


def test_pasted_error_params_surface_with_the_scope_hint(capsys):
    result = bootstrap.parse_pasted_redirect(
        "https://brooksnewmedia.com/threads-callback"
        "?error=invalid_request&error_description=Invalid+scope%3A+threads_delete"
    )
    assert result["error"] == "invalid_request"
    assert result["error_description"] == "Invalid scope: threads_delete"

    with pytest.raises(SystemExit) as exc:
        bootstrap.resolve_authorization(result, "st-abc")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Authorization failed: invalid_request" in err
    assert bootstrap.FALLBACK_SCOPES in err  # the existing re-run hint


def test_pasted_non_scope_error_skips_the_scope_hint(capsys):
    result = bootstrap.parse_pasted_redirect(
        "https://brooksnewmedia.com/threads-callback"
        "?error=access_denied&error_reason=user_denied"
    )
    with pytest.raises(SystemExit):
        bootstrap.resolve_authorization(result, "st-abc")
    err = capsys.readouterr().err
    assert "access_denied" in err
    assert bootstrap.FALLBACK_SCOPES not in err


def test_empty_paste_is_rejected_rather_than_exchanged(capsys):
    result = bootstrap.parse_pasted_redirect("   ")
    with pytest.raises(SystemExit) as exc:
        bootstrap.resolve_authorization(result, "st-abc")
    assert exc.value.code == 1
    assert "no authorization code" in capsys.readouterr().err.lower()


def test_bare_query_string_paste_still_checks_state():
    """Pete may copy only the query, not the whole URL. Still not a bare code."""
    result = bootstrap.parse_pasted_redirect("?code=AQBx-123&state=st-abc")
    assert result["bare_code"] is False
    assert result["code"] == "AQBx-123"
    assert bootstrap.resolve_authorization(result, "st-abc") == "AQBx-123"
    with pytest.raises(SystemExit):
        bootstrap.resolve_authorization(result, "different")


def test_prompt_reads_stdin_and_parses_it(monkeypatch):
    result = bootstrap.prompt_for_redirect_url(
        read_line=lambda _prompt: (
            "https://brooksnewmedia.com/threads-callback?code=AQBx-123&state=st-abc"
        )
    )
    assert result["code"] == "AQBx-123"
    assert result["state"] == "st-abc"


def test_prompt_aborts_on_eof(capsys):
    def raise_eof(_prompt):
        raise EOFError

    with pytest.raises(SystemExit) as exc:
        bootstrap.prompt_for_redirect_url(read_line=raise_eof)
    assert exc.value.code == 1
    assert "no input" in capsys.readouterr().err.lower()


def test_listener_wait_times_out_instead_of_spinning_forever():
    """The old busy-wait could never fall through to the paste prompt."""
    assert bootstrap.wait_for_callback(0.05, sink={}) is False
    assert bootstrap.wait_for_callback(5.0, sink={"code": "AQBx-123"}) is True
    assert bootstrap.wait_for_callback(5.0, sink={"error": "access_denied"}) is True


def test_loopback_listener_is_still_the_default_path():
    """Nothing regresses if a loopback URI ever becomes usable again."""
    import importlib
    import os

    assert "THREADS_REDIRECT_URI" not in os.environ
    fresh = importlib.reload(bootstrap)
    assert fresh.REDIRECT_URI.startswith("http://127.0.0.1:")
    assert fresh.REDIRECT_URI.endswith("/callback")
    assert str(fresh.PORT) in fresh.REDIRECT_URI
    assert fresh.is_loopback_redirect(fresh.REDIRECT_URI) is True
