"""Factory wrapper for integration tests needing a SignalDir-backed facade.

Uvicorn needs a module-level callable it can import as a factory. This file
provides ``make_app()`` which reads ``A2A_FACADE_SIGNAL_DIR`` from the env and
wires a ``SignalDir`` into ``create_app``. Used by fixtures in
``test_facade_integration.py`` that need to exercise ``/subscribe`` blocking
behaviour on a real uvicorn server.

Not a public API — kept under ``tests/`` so it never ships in the wheel.
"""

from __future__ import annotations

import os

from a2a_mcp_bridge.facade import create_app
from a2a_mcp_bridge.signals import SignalDir


def make_app():  # type: ignore[no-untyped-def]
    """Factory invoked by ``uvicorn tests._facade_with_signal_dir:make_app --factory``."""
    sig_dir_path = os.environ.get("A2A_FACADE_SIGNAL_DIR")
    signal_dir = SignalDir(sig_dir_path) if sig_dir_path else None
    return create_app(db_path=":memory:", signal_dir=signal_dir)
