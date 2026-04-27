"""Claim: top_level.txt is the wheel's self-attestation of what it provides.
The vault verifies each asserted name against the staged tree at add time. A
name asserted but absent is silently dropped — we'd rather under-claim than
record a binding the bytes can't honor.
"""
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble.vault import db, store

    db.init_db()

    # Stage by hand: top_level.txt asserts both 'real' and 'ghost', but only
    # 'real' has a directory. 'ghost' must not be recorded.
    staged = store.stage_dir()
    real = staged / "real"
    real.mkdir()
    (real / "__init__.py").write_text("HERE = True\n")

    di = staged / "ghostly-1.0.0.dist-info"
    di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: ghostly\nVersion: 1.0.0\n\n")
    (di / "WHEEL").write_text("Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
    (di / "top_level.txt").write_text("real\nghost\n")

    store.commit(
        name="ghostly", version="1.0.0", wheel_tag="py3-none-any",
        python_tag="py3", abi_tag="none", platform_tag="any",
        staged=staged, source="test",
    )

    conn = db.connect()
    try:
        names = [row[0] for row in conn.execute(
            "SELECT import_name FROM top_level WHERE package='ghostly'"
        )]
    finally:
        conn.close()

    assert names == ["real"], (
        f"expected only the verified name 'real', got {names}"
    )

    r.evidence.append(f"top_level.txt asserted: ['real', 'ghost']")
    r.evidence.append(f"verified subpaths exist for: ['real']")
    r.evidence.append(f"recorded in top_level: {names}")
    r.evidence.append("→ wheel self-attestation is verified, not trusted")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "top_level.txt is verified against the staged tree — asserted-but-absent "
        "names are dropped, so no row claims bytes that don't exist",
        body,
    )
