"""Locks the contract of ``scripts/restart-drevo.sh``.

The script force-recreates the local drevo container from the LOCAL image (no
pull) on the configured Bolt host port. We can't run Docker in CI, so these are
text-level assertions (mirroring ``test_run_script.py``): the script's basic
shape plus the invariants that make it correct — no ``docker pull`` (so a fresh
local build is never clobbered), a force-recreate, a host bind mount, and the
same env-var surface as run-drevo.sh.
"""

from __future__ import annotations

import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "restart-drevo.sh"
ENV_EXAMPLE = REPO_ROOT / ".drevo.env.example"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"expected {SCRIPT} to exist"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "restart-drevo.sh must be executable"


def test_script_has_bash_shebang_and_is_strict() -> None:
    text = _text()
    first = text.splitlines()[0]
    assert first.startswith("#!") and "bash" in first, "expected a bash shebang"
    assert "set -euo pipefail" in text, "script must fail fast (set -euo pipefail)"


def test_never_pulls_so_local_builds_survive() -> None:
    # The whole reason this exists next to run-drevo.sh: it must NOT pull, or a
    # freshly-built local image would be clobbered by the published one.
    assert "docker pull" not in _text(), "restart-drevo.sh must not `docker pull`"


def test_force_recreates_and_bind_mounts() -> None:
    text = _text()
    assert "docker rm -f" in text, "must force-recreate the container"
    assert "docker run -d" in text, "must run the container detached"
    assert ":/data" in text, "must bind-mount the host data dir to /data"
    assert "/health" in text, "must wait on the health endpoint"


def test_env_var_surface_matches_run_drevo() -> None:
    text = _text()
    for var in ("DREVO_NAME", "DREVO_PORT", "DREVO_BOLT_PORT", "DREVO_DATA_DIR"):
        assert var in text, f"restart-drevo.sh must honour {var} (as run-drevo.sh does)"


def test_sources_a_single_config_file() -> None:
    # A one-file config (~/.drevo.env, overridable via DREVO_ENV_FILE) makes a
    # normal restart a single bare command. It is sourced before defaults apply.
    text = _text()
    assert "DREVO_ENV_FILE" in text, "must support a DREVO_ENV_FILE config path"
    assert ".drevo.env" in text, "must default the config file to ~/.drevo.env"
    assert '. "$ENV_FILE"' in text, "must source the config file"


def test_forwards_embeddings_proxy_config_when_set() -> None:
    # The embeddings-proxy env (issue #217) must be forwarded into the container
    # when set, so POST /v1/embeddings works; a no-op when unset.
    text = _text()
    for var in (
        "DREVO_EMBEDDINGS_UPSTREAM",
        "DREVO_EMBEDDINGS_API_KEY",
        "DREVO_EMBEDDINGS_MODEL",
    ):
        assert var in text, f"restart-drevo.sh must forward {var} into the container"


def test_env_example_exists_without_a_real_key() -> None:
    # A key-less example is the only .drevo.env that belongs in git; the real
    # one (with the API key) stays out of the repo.
    assert ENV_EXAMPLE.is_file(), "a .drevo.env.example must ship for reference"
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "DREVO_EMBEDDINGS_UPSTREAM" in text and "DREVO_EMBEDDINGS_API_KEY" in text
    # The example must carry a placeholder, never a real-looking secret.
    assert "REPLACE_ME" in text, "the example key must be an obvious placeholder"
