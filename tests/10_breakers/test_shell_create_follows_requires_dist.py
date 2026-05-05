"""Claim: shell create follows the Requires-Dist closure.

Conventional intuition: pinning a single package gives you that package.
Bubble's stance: the vault has the dependency graph (METADATA's
Requires-Dist parsed into the `dependencies` table at vault-add); shell
creation should consume it. Pinning `rich-cli` produces a shell whose
closure includes `click`, `rich`, etc. — anything reachable through
the dep graph that's vaulted.

Sub-case 1 of the four-symptom seam named in issue #13.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble.run import shell as shell_mod
    from bubble.vault import db
    db.init_db()

    # Stage a dep tree: alpha -> beta -> gamma
    stage_fake_package(name="gamma", version="1.0.0", import_name="gamma",
                       init_source='VERSION = "1.0.0"\n')
    stage_fake_package(name="beta", version="1.0.0", import_name="beta",
                       init_source='VERSION = "1.0.0"\n',
                       requires_dist=["gamma"])
    stage_fake_package(name="alpha", version="1.0.0", import_name="alpha",
                       init_source='VERSION = "1.0.0"\n',
                       requires_dist=["beta"])

    sd = shell_mod.create("closuretest", ["alpha"])
    r.evidence.append(f"shell at {sd}")

    lib = sd / "lib"
    have = sorted(p.name for p in lib.iterdir())
    r.evidence.append(f"lib contents: {have}")

    # All three top-level package dirs should be linked.
    for name in ("alpha", "beta", "gamma"):
        if name not in have:
            r.error = (
                f"closure expansion did not pull in {name!r} via Requires-Dist; "
                f"got lib/={have}. Vault has the dep graph; shell create "
                f"is supposed to consume it."
            )
            return

    # Manifest should show all three pinned.
    manifest = shell_mod._read_manifest(sd)
    if set(manifest) != {"alpha", "beta", "gamma"}:
        r.error = f"manifest pinned set wrong: {sorted(manifest)}"
        return

    r.evidence.append("alpha + beta + gamma all linked from closure")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "bubble shell create follows the Requires-Dist closure: pinning a "
        "single package pulls its transitive deps into the shell, because "
        "the vault's `dependencies` table already knows the graph",
        body,
    )
