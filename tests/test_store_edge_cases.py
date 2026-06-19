"""Edge-case tests for store.py — focused on the three hardening changes.

Each test targets a specific change introduced by this branch:
  1. Atomicity — recipient SELECT + INSERT are now wrapped in
     ``BEGIN IMMEDIATE`` / ``COMMIT`` so a concurrent DELETE of the
     recipient between the two steps can no longer corrupt state. The
     transaction also makes the path robust to raise-paths (the earlier
     autocommit mode left the implicit transaction open on error).
  2. Connection timeout — ``sqlite3.connect()`` is now called with
     ``timeout=5`` so a contended writer surfaces ``OperationalError``
     (``database is locked``) instead of blocking forever.
  3. subscribe() fast-path clears the signal file — previously a
     stale signal could cause a dead-loop of fast-path calls that
     consumed nothing.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from a2a_mcp_bridge.signals import SignalDir
from a2a_mcp_bridge.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Fresh Store on a real file (so a second connection can share it)."""
    db = tmp_path / "test.sqlite"
    s = Store(str(db))
    s.init_schema()
    return s


# ---------------------------------------------------------------------------
# Change 1 — atomic recipient check + INSERT
# ---------------------------------------------------------------------------

def test_send_message_rolls_back_on_target_unknown(store: Store) -> None:
    """TARGET_UNKNOWN must leave the connection in a clean (no-tx) state.

    Before wrapping the check+INSERT in ``BEGIN IMMEDIATE`` / ``COMMIT``,
    raising ``ValueError`` from inside the implicit transaction could leave
    an autocommit-mode quirk where a subsequent write appeared to succeed
    but was not actually committed. This test asserts both: (1) the error
    is raised for an unknown recipient, and (2) the next ``send_message``
    to a valid recipient commits normally and is visible to a concurrent
    reader connection.
    """
    store.upsert_agent("alice")
    store.upsert_agent("bob")

    with pytest.raises(ValueError, match="TARGET_UNKNOWN"):
        store.send_message("alice", "nobody", "should-fail")

    # Connection must be usable afterwards — no dangling transaction.
    result = store.send_message("alice", "bob", "real message")
    assert result.message_id

    # Visible to a second connection → really committed.
    reader = sqlite3.connect(store.db_path)
    try:
        row = reader.execute(
            "SELECT body FROM messages WHERE id = ?", (result.message_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "real message"
    finally:
        reader.close()


def test_send_message_race_recipient_deleted_mid_transaction(
    store: Store,
) -> None:
    """Real race: a concurrent writer deletes the recipient between the
    SELECT and the INSERT of ``send_message``.

    Strategy: use a second connection to hold a ``BEGIN IMMEDIATE`` while
    the main thread calls ``send_message``. The main ``send_message`` must
    either commit cleanly (second connection hasn't acted yet) or fail with
    a well-defined error — never corrupt state or leave a dangling message
    row referencing a deleted agent.
    """
    store.upsert_agent("alice")
    store.upsert_agent("bob")

    # Spin a writer thread that deletes bob mid-flight under its own BEGIN.
    db_path = store.db_path
    assert db_path, "could not resolve store DB path"

    barrier = threading.Barrier(2, timeout=5)

    def deleter() -> None:
        conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            barrier.wait()  # release together with the main thread
            time.sleep(0.05)
            # Try to delete bob; the FK from messages(recipient_id) -> agents(id)
            # will block until the main thread's INSERT either commits or
            # rolls back. Either outcome is acceptable from the deleter's
            # perspective — we just need the main thread's send to remain
            # consistent.
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM messages WHERE recipient_id = 'bob'")
                conn.execute("DELETE FROM agents WHERE id = 'bob'")
                conn.execute("COMMIT")
            except sqlite3.OperationalError:
                # Timeout waiting for the main writer's lock — acceptable.
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")
        finally:
            conn.close()

    t = threading.Thread(target=deleter)
    t.start()
    barrier.wait()
    try:
        result = store.send_message("alice", "bob", "racy")
        # Success path: the message row MUST reference a still-existing bob.
        row = store._conn.execute(
            "SELECT recipient_id FROM messages WHERE id = ?",
            (result.message_id,),
        ).fetchone()
        assert row is not None
        assert row["recipient_id"] == "bob"
    except (ValueError, sqlite3.OperationalError):
        # Failure path: either TARGET_UNKNOWN (bob gone before SELECT) or
        # OperationalError (lock contention). Both are valid outcomes; the
        # invariant we care about is absence of dangling rows.
        orphans = store._conn.execute(
            "SELECT COUNT(*) FROM messages m "
            "LEFT JOIN agents a ON a.id = m.recipient_id "
            "WHERE a.id IS NULL"
        ).fetchone()
        assert orphans[0] == 0, "send_message left a dangling message row"
    finally:
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# Change 2 — connection timeout surfaces OperationalError instead of hanging
# ---------------------------------------------------------------------------

def test_connection_timeout_raises_instead_of_blocking_forever(
    store: Store,
) -> None:
    """A competing writer on the SAME DB file must surface a timeout error
    on the store's connection, not block forever.

    Uses the same DB path the store opened (so the lock is real) and holds
    a ``BEGIN IMMEDIATE`` from a second connection for longer than the
    store's 5 s ``timeout=``. The store's write must raise
    ``sqlite3.OperationalError`` within a reasonable window.
    """
    row = store._conn.execute(
        "SELECT file FROM pragma_database_list WHERE name = 'main'"
    ).fetchone()
    db_path = row[0] or store.db_path
    assert db_path, "could not resolve store DB path"

    store.upsert_agent("alice")
    store.upsert_agent("bob")

    blocker = sqlite3.connect(db_path, timeout=1, isolation_level=None)
    try:
        # BEGIN IMMEDIATE alone is enough to hold the reserved lock;
        # no further write needed (and would fight with the schema anyway).
        blocker.execute("BEGIN IMMEDIATE")

        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            store.send_message("alice", "bob", "should-timeout")
        elapsed = time.monotonic() - start
        # timeout=5 → SQLite may need up to ~12s on slow CI (macOS arm64)
        # to surface the lock error; 15s gives headroom.
        assert elapsed < 15, f"send_message blocked {elapsed:.1f}s (timeout not applied)"
    finally:
        with contextlib.suppress(sqlite3.OperationalError):
            blocker.execute("ROLLBACK")
        blocker.close()


# ---------------------------------------------------------------------------
# Change 3 — subscribe() fast-path clears the signal file
# ---------------------------------------------------------------------------

def test_subscribe_fast_path_clears_signal(tmp_path: Path) -> None:
    """After consuming messages via the fast-path, the signal file must be
    cleared so the next subscribe() doesn't fast-path a second time on a
    stale signal.
    """
    signal_dir = SignalDir(str(tmp_path / "signals"))
    s = Store(str(tmp_path / "db.sqlite"), signal_dir=signal_dir)
    s.init_schema()
    s.upsert_agent("alice")
    s.upsert_agent("bob")

    s.send_message("alice", "bob", "hello")
    signal_dir.notify("bob")

    msgs, timed_out = s.subscribe("bob", timeout_seconds=5, limit=10)
    assert len(msgs) == 1
    assert msgs[0].body == "hello"
    assert not timed_out

    signal_path = signal_dir.path / "bob.notify"
    assert not signal_path.exists(), "signal file was not cleared by fast-path"

    # Second subscribe with no new messages must time out, not fast-path.
    _, timed_out2 = s.subscribe("bob", timeout_seconds=1, limit=10)
    assert timed_out2, "expected timeout on second subscribe (no new messages)"

    s.close()


def test_subscribe_fast_path_without_signal_dir_works(store: Store) -> None:
    """Fast-path without SignalDir doesn't raise on clear (None-safe)."""
    store.upsert_agent("alice")
    store.upsert_agent("bob")
    store.send_message("alice", "bob", "hello")

    msgs, timed_out = store.subscribe("bob", timeout_seconds=5, limit=10)
    assert len(msgs) == 1
    assert not timed_out

    # Second call with no SignalDir → slow path raises (existing contract).
    with pytest.raises(RuntimeError, match="requires a SignalDir"):
        store.subscribe("bob", timeout_seconds=1, limit=10)


# ---------------------------------------------------------------------------
# Regression guards — pre-existing invariants the refactor must preserve
# ---------------------------------------------------------------------------

def test_send_to_self_still_rejected(store: Store) -> None:
    store.upsert_agent("alice")
    with pytest.raises(ValueError, match="TARGET_SELF"):
        store.send_message("alice", "alice", "msg")


def test_message_too_large_still_rejected(store: Store) -> None:
    store.upsert_agent("alice")
    store.upsert_agent("bob")
    with pytest.raises(ValueError, match="MESSAGE_TOO_LARGE"):
        store.send_message("alice", "bob", "x" * (64 * 1024 + 1))


def test_metadata_too_large_still_rejected(store: Store) -> None:
    store.upsert_agent("alice")
    store.upsert_agent("bob")
    with pytest.raises(ValueError, match="METADATA_TOO_LARGE"):
        store.send_message(
            "alice", "bob", "msg", metadata={"data": "x" * (4 * 1024)}
        )
