"""Locks the contract of ``scripts/run-drevo.sh``.

The script pulls and runs the published ``ice1x/drevo`` image the MCP talks to.
We can't spin up Docker in CI, so these are text-level assertions (mirroring
``test_compose.py``): they pin the flags that make the bind-mount, host-user,
and health-wait behaviour correct, plus the script's basic shape (executable,
``bash`` shebang, ``set -euo pipefail``, start/stop/logs dispatch).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run-drevo.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"expected {SCRIPT} to exist"


def test_script_is_executable() -> None:
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "run-drevo.sh must be executable (chmod +x)"


def test_script_has_bash_shebang() -> None:
    first = _text().splitlines()[0]
    assert first.startswith("#!") and "bash" in first, "expected a bash shebang"


def test_script_is_strict() -> None:
    assert "set -euo pipefail" in _text(), "script must fail fast (set -euo pipefail)"


def test_script_pulls_published_image() -> None:
    text = _text()
    assert "ice1x/drevo" in text, "must reference the published Docker Hub image"
    assert "docker pull" in text, "must pull the image before running"


def test_script_runs_as_host_user() -> None:
    # Without this the non-root container user can't take redb's write lock on
    # the bind-mounted file.
    assert '--user "$(id -u):$(id -g)"' in _text(), "must run the container as the host user"


def test_script_bind_mounts_data_dir() -> None:
    text = _text()
    assert "DREVO_DATA_DIR" in text, "must honour DREVO_DATA_DIR"
    assert ":/data" in text, "must bind-mount the host data dir to /data"


def test_script_publishes_http_port() -> None:
    assert ":8080" in _text(), "must publish the container's HTTP port 8080"


def test_script_waits_for_health() -> None:
    text = _text()
    assert "/health" in text, "must poll the /health endpoint"


def test_script_dispatches_subcommands() -> None:
    text = _text()
    for sub in ("start", "stop", "logs"):
        assert sub in text, f"missing '{sub}' sub-command"


def test_script_passes_shellcheck_if_available() -> None:
    """If shellcheck is installed, the script must be clean. Skipped otherwise."""
    import shutil
    import subprocess

    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        import pytest

        pytest.skip("shellcheck not installed")
    proc = subprocess.run(
        [shellcheck, str(SCRIPT)],
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"shellcheck failed:\n{proc.stdout}\n{proc.stderr}"
