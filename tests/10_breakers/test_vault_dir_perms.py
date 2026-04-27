"""Claim: ensure_dirs creates the vault tree with 0o700 permissions.

Conventional intuition: the vault is "just packages I downloaded from
PyPI"; world-readable is fine. In practice wheels regularly include
example credentials in dist-info, partial tokens in test fixtures, and
miscellaneous metadata users did not consent to making world-readable.
Defense in depth: the whole tree is u=rwx,go= by default. If the user
chooses wider mode for an existing dir, we don't widen the existing dir
back down to 0o700 — only newly-created dirs are bound by the default.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble import config
    config.ensure_dirs()
    for d in (config.BUBBLE_HOME, config.VAULT_DIR, config.STAGING_DIR,
              config.SHELLS_DIR, config.WHEELS_DIR, config.LOGS_DIR):
        st = d.stat()
        mode = st.st_mode & 0o777
        assert mode == 0o700, f"{d}: mode is 0o{mode:o}, expected 0o700"
        r.evidence.append(f"  {d.name:18s} mode=0o{mode:o}")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "ensure_dirs creates BUBBLE_HOME, vault, staging, shells, wheels, "
        "logs at 0o700 — wheel payloads are not in general world-readable, "
        "and the vault should match",
        body,
    )
