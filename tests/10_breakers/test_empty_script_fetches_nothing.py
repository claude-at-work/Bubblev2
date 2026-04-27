"""Claim: a script that imports nothing leaves the vault empty.

Conventional intuition: any isolation system has a setup cost. venv pre-installs
pip and wheels even if you never use them; conda solves a graph before you've
written a line. Bubble is demand-paged — pay for what you touch, nothing more.

Proof shape: install the VaultFinder with autofetch on, run trivial code that
imports only stdlib, then count vault entries. Should be zero.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, vault_finder, Result


def body(r: Result):
    from bubble import config
    from bubble.vault import db, store

    db.init_db()

    with vault_finder(autofetch=True):
        # The most aggressive thing a "do nothing" script could do —
        # import ten different stdlib modules. Vault should still be empty.
        import json, re, os, sys as _sys, math, hashlib, sqlite3 as _sq
        import collections, itertools, functools  # noqa: F401
        result = json.dumps({"x": 1})
        assert result == '{"x": 1}'

    conn = db.connect()
    try:
        pkg_count = conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
        wheels_count = sum(1 for _ in config.WHEELS_DIR.iterdir()) \
                       if config.WHEELS_DIR.exists() else 0
    finally:
        conn.close()

    assert pkg_count == 0, f"vault grew to {pkg_count} packages from a stdlib-only run"
    assert wheels_count == 0, f"wheels dir grew to {wheels_count} files"

    r.evidence.append(f"stdlib imports: 10")
    r.evidence.append(f"vault.packages rows: {pkg_count}")
    r.evidence.append(f"wheels/ entries:    {wheels_count}")
    r.evidence.append("→ demand paging: zero touched, zero paid")
    r.passed = True


if __name__ == "__main__":
    run_test("a stdlib-only run with autofetch on leaves the vault empty", body)
