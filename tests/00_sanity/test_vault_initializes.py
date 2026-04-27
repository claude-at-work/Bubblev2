"""Sanity: a fresh BUBBLE_HOME yields a usable vault DB with the new schema.

Boring but necessary. If this fails, every other test is meaningless.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble import config
    from bubble.vault import db

    db.init_db()

    assert config.VAULT_DB.exists(), "vault.db not created"
    conn = db.connect()
    try:
        rows = list(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ))
    finally:
        conn.close()
    table_names = [row[0] for row in rows]
    expected = {"packages", "top_level", "modules", "dependencies",
                "module_imports", "bubbles", "shells", "schema_meta"}
    missing = expected - set(table_names)
    assert not missing, f"missing tables: {missing}"

    cols_rows = list(db.connect().execute("PRAGMA table_info(packages)"))
    pk_cols = {row[1] for row in cols_rows if row[5]}
    assert pk_cols == {"name", "version", "wheel_tag"}, \
        f"packages PK should be (name, version, wheel_tag), got {pk_cols}"

    r.evidence.append(f"vault_db: {config.VAULT_DB}")
    r.evidence.append(f"tables: {len(table_names)} ({', '.join(sorted(table_names))})")
    r.evidence.append(f"packages PK: {sorted(pk_cols)}")
    r.passed = True


if __name__ == "__main__":
    run_test("a fresh BUBBLE_HOME yields a usable vault DB on schema v2", body)
