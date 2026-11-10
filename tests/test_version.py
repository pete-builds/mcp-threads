"""Every place this repo states its version must state the same one.

``release.yml`` checks only the image tag in ``docker-compose.yml`` against
the git tag being released. Nothing checked the other two, and they drifted:
the 0.2.1 and 0.2.2 images were built from a ``pyproject.toml`` that said so
while ``server.py`` still said ``0.2.0``, which is what ``GET /healthz``
reported from both. Bump all three together or this goes red.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/pete-builds/mcp-threads"


def _pyproject_version() -> str:
    return tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]


def _server_version() -> str:
    """Read ``VERSION`` from the source, not by importing: importing ``server``
    builds a client and reads the token store."""
    for node in ast.parse((REPO / "server.py").read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("server.py no longer assigns VERSION at module level")


def _compose_image() -> str:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    return compose["services"]["mcp-threads"]["image"]


def test_version_surfaces_agree():
    version = _pyproject_version()
    assert _server_version() == version, "server.py VERSION is what /healthz reports"
    assert _compose_image() == f"{IMAGE}:{version}", "release.yml fails closed on this one"


def test_the_version_is_a_plain_release_number():
    """A control on the readers above: each returns a real x.y.z, so the
    equality is not two empty strings agreeing."""
    for value in (_pyproject_version(), _server_version(), _compose_image().rsplit(":", 1)[1]):
        parts = value.split(".")
        assert len(parts) == 3 and all(part.isdigit() for part in parts), value
