"""Guardrail: the version advertised by `pyproject.toml` and the one imported
from ``a2a_mcp_bridge.__version__`` must agree.

Rationale
---------
The package version lives in two places:

1. ``pyproject.toml[project].version`` — what build tooling (uv, pip, wheel
   metadata, ``importlib.metadata.version("a2a-mcp-bridge")``) sees.
2. ``src/a2a_mcp_bridge/__init__.py::__version__`` — what the running
   process actually advertises (e.g. the facade ``/health`` endpoint and
   the ``agent_ping`` MCP tool both read ``__version__``).

If these drift (e.g. a PR bumps one and forgets the other), the facade
will serve a stale version string even after a successful upgrade,
making deployment verification ambiguous. This has happened twice in
our release history (before v0.7.6), each time costing a retag + new
release to correct.

This test is fast, has no external dependencies, and runs in every CI
matrix job, so the drift is caught at PR time rather than at release
time.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version as metadata_version
from pathlib import Path

from a2a_mcp_bridge import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _read_pyproject_version() -> str:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


def test_pyproject_version_matches_dunder_version() -> None:
    """pyproject.toml[project].version == a2a_mcp_bridge.__version__."""
    pyproject_version = _read_pyproject_version()
    assert pyproject_version == __version__, (
        f"Version drift detected between pyproject.toml and "
        f"src/a2a_mcp_bridge/__init__.py.\n"
        f"  pyproject.toml[project].version = {pyproject_version!r}\n"
        f"  a2a_mcp_bridge.__version__       = {__version__!r}\n"
        f"Bump both when releasing a new version."
    )


def test_installed_metadata_version_matches_dunder_version() -> None:
    """importlib.metadata.version('a2a-mcp-bridge') == __version__.

    Catches the case where the package is installed from a wheel built
    against a different pyproject.toml than the source tree on disk
    (e.g. stale uv cache, partial reinstall).
    """
    try:
        installed = metadata_version("a2a-mcp-bridge")
    except Exception as exc:  # pragma: no cover - defensive
        # Package not installed — only happens if tests are run without
        # `pip install -e .`. Skip rather than fail to stay friendly in
        # that edge case.
        import pytest

        pytest.skip(f"a2a-mcp-bridge not installed in this environment: {exc}")

    assert installed == __version__, (
        f"Installed metadata version disagrees with __version__.\n"
        f"  importlib.metadata.version('a2a-mcp-bridge') = {installed!r}\n"
        f"  a2a_mcp_bridge.__version__                   = {__version__!r}\n"
        f"Hint: reinstall with `uv pip install -e .` to rebuild metadata."
    )
