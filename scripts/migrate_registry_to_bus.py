#!/usr/bin/env python3
"""Migration script: Capability Registry → Bus SQLite (ADR-008)."""

import argparse, sqlite3
from pathlib import Path

def get_db_connection(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def stage1_detect_validate(legacy_path, bus_path):
    if not legacy_path.exists():
        return {"status": "skipped", "reason": f"Legacy file not found: {legacy_path}", "count": 0}
    conn = get_db_connection(legacy_path)
    count = conn.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0]
    try:
        get_db_connection(bus_path).execute("SELECT 1 FROM capabilities LIMIT 1")
    except sqlite3.OperationalError:
        raise RuntimeError(f"Bus DB {bus_path} lacks capabilities table")
    conn.close()
    return {"status": "ready", "count": count, "legacy_path": legacy_path}

def stage2_insert_ignore(legacy_path, bus_path):
    legacy_conn = get_db_connection(legacy_path)
    bus_conn = get_db_connection(bus_path)
    bus_conn.execute("BEGIN IMMEDIATE")
    try:
        rows = legacy_conn.execute("SELECT agent_id, skill_id, domain, description, monetary_cost_usd, tokens_per_call, announced_at FROM capabilities").fetchall()
        inserted = 0
        for row in rows:
            bus_conn.execute("INSERT OR IGNORE INTO capabilities (agent_id, skill_id, domain, description, monetary_cost_usd, tokens_per_call, announced_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row[0], row[1], row[2] or "general", row[3], row[4], row[5] or 0, row[6]))
            inserted += 1
        bus_conn.execute("COMMIT")
    except Exception:
        bus_conn.execute("ROLLBACK")
        raise
    finally:
        legacy_conn.close()
        bus_conn.close()
    return inserted

def stage3_delete_legacy(legacy_path):
    if not legacy_path.exists(): return False
    for ext in [".wal", ".shm"]:
        (Path(str(legacy_path)+ext)).unlink(missing_ok=True)
    legacy_path.unlink()
    return True

def main():
    parser = argparse.ArgumentParser(description="Migrate Capability Registry to Bus SQLite (ADR-008)")
    parser.add_argument("--bus-path", default="data/a2a-bus.sqlite")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    bus_path = Path(args.bus_path).resolve()
    legacy_path = Path(str(bus_path.with_suffix(""))).with_suffix(".registry.db")
    
    print(f"Migration: {legacy_path} -> {bus_path}")
    r = stage1_detect_validate(legacy_path, bus_path)
    if r["status"]=="skipped":
        print("Skipped:", r["reason"])
        return
    print(f"Stage 1: Found {r['count']} capabilities")
    if args.dry_run:
        print("DRY RUN mode, stopping here")
        return
    
    ins = stage2_insert_ignore(legacy_path, bus_path)
    print(f"Stage 2: Inserted {ins} rows")
    if stage3_delete_legacy(legacy_path):
        print(f"Stage 3: Deleted {legacy_path}")
    print("Migration complete.")

if __name__ == "__main__":
    main()
