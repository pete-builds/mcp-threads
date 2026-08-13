"""Image-post validation.

Meta fetches ``image_url`` **from its own servers**, so the only things worth
checking locally are the ones that make that fetch impossible or pointless:

* the URL must be absolute http(s) — a local path or a data URI can never work
* the host must be publicly routable — loopback, RFC1918, link-local and
  ``.local`` names resolve to something on *our* network, not Meta's
* if the path carries a file extension it must be JPEG or PNG

Everything else Meta documents (8 MB maximum, aspect ratio at most 10:1, width
320 to 1440, sRGB) can only be known by downloading the bytes. This server
deliberately does **not** fetch arbitrary caller-supplied URLs: that is an SSRF
surface and a slow one. Those limits are enforced by Meta during container
processing and surface through ``get_container_status`` as ``error_message``
values such as ``INVALID_ASPECT_RATIO``.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from clients.errors import ThreadsInputError

ALT_TEXT_LIMIT = 1000
ALLOWED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")

#: Documented, enforced by Meta not by us. Surfaced in tool docstrings.
IMAGE_SPECS = {
    "formats": ["JPEG", "PNG"],
    "max_bytes": 8 * 1024 * 1024,
    "max_aspect_ratio": "10:1",
    "min_width": 320,
    "max_width": 1440,
    "color_space": "sRGB",
    "enforced_by": "Meta during container processing, not by this server",
}

_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home", ".localdomain")


def _is_private_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if not host or host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not addr.is_global


def validate_image_url(url: str) -> str:
    """Reject URLs Meta's servers could never usefully fetch. Returns the URL."""
    if not url or not str(url).strip():
        raise ThreadsInputError("image_url is empty.")
    url = str(url).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ThreadsInputError(
            "image_url must be an absolute http(s) URL. Meta downloads the image "
            "from its own servers, so a local file path or data URI cannot work.",
            details={"scheme": parsed.scheme or None},
        )
    if not parsed.hostname:
        raise ThreadsInputError("image_url has no host.", details={"url_scheme": parsed.scheme})
    if _is_private_host(parsed.hostname):
        raise ThreadsInputError(
            f"image_url points at {parsed.hostname}, which is not reachable from the "
            "public internet. Meta fetches the image server-side, so it must be "
            "hosted somewhere publicly addressable.",
            details={"host": parsed.hostname},
        )
    path = parsed.path.lower()
    dot = path.rfind(".")
    if dot != -1 and dot > path.rfind("/"):
        ext = path[dot:]
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            raise ThreadsInputError(
                f"Threads accepts JPEG and PNG images; image_url ends in {ext}.",
                details={"extension": ext, "allowed": list(ALLOWED_IMAGE_EXTENSIONS)},
            )
    return url


def validate_alt_text(alt_text: str | None) -> str | None:
    if alt_text is None:
        return None
    text = str(alt_text)
    if len(text) > ALT_TEXT_LIMIT:
        raise ThreadsInputError(
            f"alt_text is {len(text)} characters; the maximum is {ALT_TEXT_LIMIT}.",
            details={"length": len(text), "limit": ALT_TEXT_LIMIT},
        )
    return text
