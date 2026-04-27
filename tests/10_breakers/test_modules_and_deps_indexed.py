"""Claim: vault-add populates modules / module_imports / dependencies, not
just packages and top_level.

Conventional intuition: a vault that knows about each package's modules,
their imports, and its declared deps can answer questions about the
shape of what it serves — not just 'is this name in the vault?'.

For most of the project's life these three tables existed in the schema
but were never written. A live vault held 64 packages with `modules: 0,
module_imports: 0, dependencies: 0`. This test pins the indexer down so
the schema and the writes don't drift apart again.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble import config


    stage_fake_package(
        name="gizmo",
        version="0.1.0",
        import_name="gizmo",
        init_source='''
            from gizmo.helpers import shout
            import json
            import requests
            VERSION = "0.1.0"
        ''',
        submodules={
            "helpers": '''
                import os
                from urllib3 import PoolManager
                def shout(s): return s.upper()
            ''',
        },
        requires_dist=[
            "requests<3,>=2.0",
            'urllib3 (>=1.21.1); python_version >= "3.5"',
            'pytest; extra == "test"',
        ],
    )

    conn = sqlite3.connect(str(config.VAULT_DB))

    # modules: gizmo (from __init__.py) + gizmo.helpers
    mods = {row[0] for row in conn.execute(
        "SELECT module_name FROM modules WHERE package=?", ("gizmo",)
    )}
    assert mods == {"gizmo", "gizmo.helpers"}, f"modules: {mods}"
    r.evidence.append(f"modules: {sorted(mods)}")

    # module_imports: external imports of each module exclude stdlib (`os`,
    # `json`) and own-package (`gizmo`).
    rows = {row[0]: json.loads(row[1]) for row in conn.execute(
        "SELECT module_name, imports_external FROM module_imports WHERE package=?",
        ("gizmo",),
    )}
    assert rows.get("gizmo") == ["requests"], \
        f"gizmo external imports: {rows.get('gizmo')}"
    assert rows.get("gizmo.helpers") == ["urllib3"], \
        f"gizmo.helpers external imports: {rows.get('gizmo.helpers')}"
    r.evidence.append(f"gizmo            external imports: {rows.get('gizmo')}")
    r.evidence.append(f"gizmo.helpers    external imports: {rows.get('gizmo.helpers')}")

    # dependencies: 3 entries, one optional with extra='test'.
    deps = sorted(conn.execute(
        "SELECT dep_name, dep_version_spec, optional, extra "
        "FROM dependencies WHERE package=?",
        ("gizmo",),
    ))
    assert deps == [
        ("pytest", "", 1, "test"),
        ("requests", "<3,>=2.0", 0, None),
        ("urllib3", ">=1.21.1", 0, None),
    ], f"deps: {deps}"
    r.evidence.append("dependencies parsed: name + spec + optional + extra")
    for d in deps:
        r.evidence.append(f"  {d}")

    # Idempotency: re-staging the same key with overwrite=True replaces the
    # tree AND its index rows — no leftover dependency rows from the prior
    # add, no duplicate module rows.
    stage_fake_package(
        name="gizmo",
        version="0.1.0",
        import_name="gizmo",
        init_source="VERSION = '0.1.0-rebuilt'",
        requires_dist=["requests<3,>=2.0"],
        overwrite=True,
    )
    conn = sqlite3.connect(str(config.VAULT_DB))  # reconnect after commit
    n_mods = conn.execute(
        "SELECT COUNT(*) FROM modules WHERE package=?", ("gizmo",)
    ).fetchone()[0]
    n_deps = conn.execute(
        "SELECT COUNT(*) FROM dependencies WHERE package=?", ("gizmo",)
    ).fetchone()[0]
    assert n_mods == 1, f"after re-stage: {n_mods} modules (expected 1)"
    assert n_deps == 1, f"after re-stage: {n_deps} deps (expected 1)"
    r.evidence.append("re-stage overwrites: no duplicate rows under same key")

    conn.close()
    r.passed = True


if __name__ == "__main__":
    run_test(
        "vault-add populates modules, module_imports (split into stdlib-and-own"
        "-pkg-filtered externals), and dependencies (Requires-Dist parsed) — "
        "the three tables that schema v2 declared but never wrote",
        body,
    )
