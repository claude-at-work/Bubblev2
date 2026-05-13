"""bubble project: BUBBLE_SHELL makes VaultFinder honour per-project version pins."""
import os
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result, stage_fake_package


def body(r: Result):
    from bubble.vault import db
    from bubble.run import shell as shell_mod
    from bubble.meta_finder import VaultFinder
    from bubble import config

    db.init_db()
    stage_fake_package(name="charlie", version="1.0.0", import_name="charlie",
                       init_source='VERSION = "1.0.0"')
    stage_fake_package(name="charlie", version="2.0.0", import_name="charlie",
                       init_source='VERSION = "2.0.0"', overwrite=True)

    # proj-a pins 1.0.0; proj-b pins 2.0.0
    shell_mod.create("proj-a", [], exist_ok=True)
    shell_mod.add_pinned("proj-a", "charlie", "1.0.0", "py3-none-any")

    shell_mod.create("proj-b", [], exist_ok=True)
    shell_mod.add_pinned("proj-b", "charlie", "2.0.0", "py3-none-any")

    old_env = os.environ.get("BUBBLE_SHELL")
    try:
        os.environ["BUBBLE_SHELL"] = "proj-a"
        finder_a = VaultFinder()
        conn = sqlite3.connect(str(config.VAULT_DB))
        row_a = finder_a._query_vault(conn, "charlie", "charlie")
        conn.close()
        r.evidence.append(f"proj-a charlie: {row_a[1] if row_a else None}")
        assert row_a is not None, "proj-a: charlie not found"
        assert row_a[1] == "1.0.0", f"proj-a should see 1.0.0, got {row_a[1]}"

        os.environ["BUBBLE_SHELL"] = "proj-b"
        finder_b = VaultFinder()
        conn = sqlite3.connect(str(config.VAULT_DB))
        row_b = finder_b._query_vault(conn, "charlie", "charlie")
        conn.close()
        r.evidence.append(f"proj-b charlie: {row_b[1] if row_b else None}")
        assert row_b is not None, "proj-b: charlie not found"
        assert row_b[1] == "2.0.0", f"proj-b should see 2.0.0, got {row_b[1]}"
    finally:
        if old_env is None:
            os.environ.pop("BUBBLE_SHELL", None)
        else:
            os.environ["BUBBLE_SHELL"] = old_env

    r.passed = True


if __name__ == "__main__":
    run_test("BUBBLE_SHELL pins project-specific version (soft vault isolation)", body)
