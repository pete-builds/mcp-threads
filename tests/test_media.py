"""Image posts, container status polling, and replying to anyone's post."""

from __future__ import annotations

import httpx
import pytest
import respx

from clients.errors import ThreadsInputError
from clients.media import ALT_TEXT_LIMIT, validate_alt_text, validate_image_url
from tests.test_client import GRAPH, SEED, mock_publishing_limit, seed_volume
from tests.test_tools import build, call

GOOD = "https://cdn.brooksnewmedia.com/shots/rack.jpg"


def mock_profile():
    return respx.get(f"{GRAPH}/me").mock(
        return_value=httpx.Response(200, json={"id": "77", "username": "u"})
    )


# --- URL validation (cheap checks only; Meta enforces the rest) --------


def test_a_public_jpeg_or_png_url_passes():
    assert validate_image_url(GOOD) == GOOD
    validate_image_url("http://example.com/a.PNG")
    validate_image_url("https://example.com/image")  # extensionless CDN URL


@pytest.mark.parametrize(
    "url",
    [
        "/Users/ps959/photo.jpg",
        "file:///tmp/photo.jpg",
        "data:image/png;base64,iVBORw0KGgo=",
    ],
)
def test_non_http_urls_are_rejected(url):
    with pytest.raises(ThreadsInputError, match="absolute http"):
        validate_image_url(url)


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "192.168.86.20", "10.0.0.5", "nix1.local", "[::1]"],
)
def test_urls_meta_could_never_reach_are_rejected(host):
    with pytest.raises(ThreadsInputError, match="not reachable from the public internet"):
        validate_image_url(f"http://{host}/photo.jpg")


def test_a_non_image_extension_is_rejected():
    with pytest.raises(ThreadsInputError, match="JPEG and PNG"):
        validate_image_url("https://example.com/clip.mp4")
    with pytest.raises(ThreadsInputError, match="JPEG and PNG"):
        validate_image_url("https://example.com/photo.gif")


def test_alt_text_is_capped_at_1000_characters():
    assert validate_alt_text("x" * ALT_TEXT_LIMIT)
    with pytest.raises(ThreadsInputError, match="maximum is 1000"):
        validate_alt_text("x" * (ALT_TEXT_LIMIT + 1))


# --- image containers --------------------------------------------------


@respx.mock
async def test_an_image_post_sends_media_type_image(tmp_path):
    mock_profile()
    create = respx.post(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(200, json={"id": "c-img"})
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(
        mcp, "create_post", text="new rack day", image_url=GOOD, alt_text="a server rack"
    )

    params = dict(create.calls[0].request.url.params)
    assert params["media_type"] == "IMAGE"
    assert params["image_url"] == GOOD
    assert params["alt_text"] == "a server rack"
    assert params["text"] == "new rack day"
    assert "auto_publish_text" not in params
    assert out["data"]["requires_status_check"] is True
    assert out["data"]["media_type"] == "IMAGE"
    await client.close()


@respx.mock
async def test_an_image_post_does_not_require_text(tmp_path):
    mock_profile()
    create = respx.post(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(200, json={"id": "c-img"})
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "create_post", image_url=GOOD)
    assert out["data"]["creation_id"] == "c-img"
    assert "text" not in dict(create.calls[0].request.url.params)
    await client.close()


@respx.mock
async def test_a_text_post_still_requires_text(tmp_path):
    route = respx.post(f"{GRAPH}/77/threads")
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "create_post")
    assert out["code"] == "INVALID_INPUT"
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_a_text_post_is_publishable_immediately(tmp_path):
    mock_profile()
    respx.post(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(200, json={"id": "c-text"})
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "create_post", text="just words")
    assert out["data"]["media_type"] == "TEXT"
    assert out["data"]["requires_status_check"] is False
    await client.close()


@respx.mock
async def test_alt_text_without_an_image_is_rejected(tmp_path):
    route = respx.post(f"{GRAPH}/77/threads")
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "create_post", text="hi", alt_text="describes nothing")
    assert out["code"] == "INVALID_INPUT"
    assert route.call_count == 0
    await client.close()


@respx.mock
async def test_a_bad_image_url_never_reaches_the_api(tmp_path):
    route = respx.post(f"{GRAPH}/77/threads")
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "create_post", image_url="http://192.168.86.20/x.jpg")
    assert out["code"] == "INVALID_INPUT"
    assert route.call_count == 0
    await client.close()


# --- container status --------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("status", "ready"),
    [
        ("FINISHED", True),
        ("IN_PROGRESS", False),
        ("ERROR", False),
        ("EXPIRED", False),
        ("PUBLISHED", False),
    ],
)
async def test_every_container_status_gets_guidance(tmp_path, status, ready):
    respx.get(f"{GRAPH}/c-1").mock(
        return_value=httpx.Response(200, json={"id": "c-1", "status": status})
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "get_container_status", container_id="c-1")
    assert out["data"]["status"] == status
    assert out["data"]["ready_to_publish"] is ready
    assert out["data"]["guidance"]
    await client.close()


@respx.mock
async def test_a_processing_error_surfaces_metas_error_message(tmp_path):
    respx.get(f"{GRAPH}/c-1").mock(
        return_value=httpx.Response(
            200,
            json={"id": "c-1", "status": "ERROR", "error_message": "INVALID_ASPECT_RATIO"},
        )
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "get_container_status", container_id="c-1")
    assert out["data"]["error_message"] == "INVALID_ASPECT_RATIO"
    assert "INVALID_ASPECT_RATIO" in out["data"]["guidance"]
    await client.close()


@respx.mock
async def test_a_failed_publish_attaches_the_container_error(tmp_path):
    """The useful string lives on the container, not on the publish response."""
    mock_profile()
    mock_publishing_limit()
    respx.post(f"{GRAPH}/77/threads_publish").mock(
        return_value=httpx.Response(400, json={"error": {"message": "media not ready"}})
    )
    respx.get(f"{GRAPH}/c-img").mock(
        return_value=httpx.Response(
            200,
            json={"id": "c-img", "status": "ERROR", "error_message": "INVALID_ASPECT_RATIO"},
        )
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    out = await call(mcp, "publish_post", creation_id="c-img")
    assert out["code"] == "INVALID_INPUT"
    assert out["details"]["container_status"] == "ERROR"
    assert out["details"]["container_error_message"] == "INVALID_ASPECT_RATIO"
    await client.close()


# --- replying to anyone's post -----------------------------------------


@respx.mock
async def test_replying_to_someone_elses_post_is_the_same_two_step_flow(tmp_path):
    mock_profile()
    mock_publishing_limit()
    create = respx.post(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(200, json={"id": "c-reply"})
    )
    publish = respx.post(f"{GRAPH}/77/threads_publish").mock(
        return_value=httpx.Response(200, json={"id": "m-reply"})
    )
    respx.get(f"{GRAPH}/m-reply").mock(
        return_value=httpx.Response(
            200, json={"id": "m-reply", "permalink": "https://threads.net/p/r"}
        )
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)

    created = await call(
        mcp, "create_post", text="Good thread.", reply_to_id="somebody-elses-media-id"
    )
    assert created["data"]["is_reply"] is True
    assert created["data"]["quota_consumed_on_publish"] == "replies"
    params = dict(create.calls[0].request.url.params)
    assert params["reply_to_id"] == "somebody-elses-media-id"
    assert params["media_type"] == "TEXT"

    # Nothing is live until the second step. That boundary is the safety gate.
    assert publish.call_count == 0

    published = await call(mcp, "publish_post", creation_id="c-reply")
    assert published["data"]["media_id"] == "m-reply"
    assert published["data"]["quota_kind"] == "replies"
    assert client.publish_log.used("replies") == 1
    assert client.publish_log.used("posts") == 0
    await client.close()


@respx.mock
async def test_a_reply_is_refused_when_the_reply_quota_is_gone(tmp_path):
    mock_profile()
    mock_publishing_limit(reply_quota_usage=1000)
    route = respx.post(f"{GRAPH}/77/threads_publish")
    respx.post(f"{GRAPH}/77/threads").mock(
        return_value=httpx.Response(200, json={"id": "c-reply"})
    )
    seed_volume(tmp_path, SEED, age_days=5)
    mcp, client = build(tmp_path)
    await call(mcp, "create_post", text="hi", reply_to_id="x")
    out = await call(mcp, "publish_post", creation_id="c-reply")
    assert out["code"] == "RATE_LIMITED"
    assert "replies: need 1, 0 left" in out["error"]
    assert route.call_count == 0
    await client.close()
