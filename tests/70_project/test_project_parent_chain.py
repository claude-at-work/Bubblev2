"""bubble project: spinoff shell inherits parent scope for unlisted packages."""
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
    stage_fake_package(name="delta", version="3.0.0", import_name="delta")
    stage_fake_package(name="echo-pkg", version="4.0.0", import_name="echo_pkg")

    # Parent shell: owns both delta and echo_pkg
    shell_mod.create("parent-shell", [], exist_ok=True)
    shell_mod.add_pinned("parent-shell", "delta", "3.0.0", "py3-none-any")
    shell_mod.add_pinned("parent-shell", "echo-pkg", "4.0.0", "py3-none-any")

    # Spinoff: only pins delta explicitly; echo_pkg should come from parent
    shell_mod.create("spinoff-shell", [], exist_ok=True, parent="parent-shell")
    shell_mod.add_pinned("spinoff-shell", "delta", "3.0.0", "py3-none-any")

    old_env = os.environ.get("BUBBLE_SHELL")
    try:
        os.environ["BUBBLE_SHELL"] = "spinoff-shell"
        finder = VaultFinder()
        conn = sqlite3.connect(str(config.VAULT_DB))

        row_delta = finder._query_vault(conn, "delta", "delta")
        r.evidence.append(f"spinoff delta: {row_delta}")
        assert row_delta is not None, "delta not found in spinoff"
        assert row_delta[1] == "3.0.0", f"delta version wrong: {row_delta[1]}"

        # echo_pkg is not pinned by spinoff but IS in parent scope
        row_echo = finder._query_vault(conn, "echo_pkg", "echo-pkg")
        r.evidence.append(f"spinoff echo_pkg (inherited): {row_echo}")
        assert row_echo is not None, "echo_pkg not inherited from parent"
        assert row_echo[1] == "4.0.0", f"echo_pkg version wrong: {row_echo[1]}"

        conn.close()
    finally:
        if old_env is None:
            os.environ.pop("BUBBLE_SHELL", None)
        else:
            os.environ["BUBBLE_SHELL"] = old_env

    r.passed = True


if __name__ == "__main__":
    run_test("spinoff shell inherits parent scope for packages it does not pin", body)
