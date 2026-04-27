"""Claim: `import yaml` resolves to the package distributed as `pyyaml`,
without consulting any hardcoded import-name → dist-name table.

Conventional intuition: the PIL/Pillow, yaml/PyYAML, cv2/opencv-python gap
needs a manual mapping that someone has to maintain. The legacy bubble.py
had a hardcoded IMPORT_TO_PACKAGE dict for exactly this. The new design says
the dist-info itself tells you (via top_level.txt), so the SQLite top_level
index is the bridge — no curated table needed.

Proof shape: stage a synthetic package whose distribution name differs from
its top-level import name. Then `import <top_level_name>`. The finder must
resolve it via the index alone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


def body(r: Result):
    from bubble.vault import db

    # Distribution is "Carbohydrate-9000", but the importable module is "sugar".
    # No human-curated table connects them. Only the dist-info's top_level.txt.
    name, version, tag, vault_path = stage_fake_package(
        name="Carbohydrate-9000",
        version="3.0.0",
        import_name="sugar",
        init_source='WHO_AM_I = "sugar from Carbohydrate-9000"',
    )

    conn = db.connect()
    try:
        idx_rows = list(conn.execute(
            "SELECT package, version, import_name FROM top_level "
            "WHERE import_name = ?",
            ("sugar",),
        ))
    finally:
        conn.close()

    assert idx_rows, "top_level index missing the sugar→Carbohydrate-9000 row"

    with vault_finder():
        import sugar  # type: ignore
        assert sugar.WHO_AM_I == "sugar from Carbohydrate-9000"

    r.evidence.append(f"distribution name: {name}")
    r.evidence.append(f"top-level import:  sugar")
    r.evidence.append(f"top_level row:     {idx_rows[0]}")
    r.evidence.append(f"resolved module:   {sugar.__file__}")
    r.evidence.append("→ no hardcoded mapping needed; the dist-info IS the mapping")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "import name resolves to a different distribution name via the "
        "SQLite top_level index, with no hardcoded table",
        body,
    )
