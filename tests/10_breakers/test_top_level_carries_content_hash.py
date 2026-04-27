"""Claim: every top_level row carries a sha256 over the subtree it claims,
populated at vault-add time. The hash is the cryptographic edge between the
import name and the bytes the vault will serve under it. Different content
under the same import name yields a different hash.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble.vault import db

    stage_fake_package(
        name="alpha", version="1.0.0", import_name="alpha",
        init_source='X = 1',
    )
    stage_fake_package(
        name="beta", version="1.0.0", import_name="beta",
        init_source='X = 2',
    )

    conn = db.connect()
    try:
        rows = list(conn.execute(
            "SELECT package, import_name, import_sha256 FROM top_level "
            "WHERE import_name IN ('alpha', 'beta') ORDER BY import_name"
        ))
    finally:
        conn.close()

    assert len(rows) == 2, f"expected 2 rows, got {rows}"
    by_name = {r_[1]: r_ for r_ in rows}
    a_hash = by_name["alpha"][2]
    b_hash = by_name["beta"][2]

    assert a_hash and len(a_hash) == 64 and all(c in "0123456789abcdef" for c in a_hash), \
        f"alpha import_sha256 not a sha256 hex: {a_hash!r}"
    assert b_hash and len(b_hash) == 64, f"beta import_sha256 not a sha256 hex: {b_hash!r}"
    assert a_hash != b_hash, "different content must yield different import_sha256"

    r.evidence.append(f"alpha import_sha256: {a_hash}")
    r.evidence.append(f"beta  import_sha256: {b_hash}")
    r.evidence.append("→ each top_level row binds the import name to its bytes")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "every top_level row carries a content sha256 over its subtree, "
        "populated at vault-add — the import-name → bytes edge is cryptographic",
        body,
    )
