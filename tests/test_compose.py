"""Locks Deliverable 1: docker-compose.yml bind-mounts a host folder for /data.

Kept in this package so the whole test surface runs on the fast Python path.
Reads the repo-root ``docker-compose.yml`` that brings up the published
``ice1x/drevo`` image this MCP talks to over HTTP.
"""

from __future__ import annotations

from pathlib import Path

# In this standalone repo the package sits at the root, so the compose file is
# one level up from ``tests/`` (was ``parents[3]`` inside the monorepo).
REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"


def test_compose_file_exists() -> None:
    assert COMPOSE.is_file(), f"expected {COMPOSE} to exist"


def test_compose_bind_mounts_host_folder() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${DREVO_DATA_DIR:-./data}:/data" in text, "compose must bind-mount the host data dir"


def test_compose_runs_as_host_user() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${DREVO_UID:-1000}:${DREVO_GID:-1000}" in text, "compose must run as the host user"


def test_compose_has_no_named_volume() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert (
        "drevo-data:" not in text
    ), "the named-volume declaration must be gone (bind-mount instead)"


def test_compose_pulls_published_image() -> None:
    # This standalone repo pulls the prebuilt Docker Hub image; it must NOT try
    # to build from a local Dockerfile (there is none here).
    text = COMPOSE.read_text(encoding="utf-8")
    assert "image: ice1x/drevo" in text, "compose must use the published ice1x/drevo image"
    assert "build:" not in text, "compose must not build locally (no Dockerfile in this repo)"


def test_compose_publishes_http_port() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert '"8080:8080"' in text, "compose must publish the HTTP API on 8080"
