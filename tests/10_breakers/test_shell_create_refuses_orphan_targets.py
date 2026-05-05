"""Claim: shell creation refuses to pin a (name, version, wheel_tag)
that isn't in the vault, with shell_pkg_missing recorded to host.toml.

Conventional intuition: a missing pin produces a half-shaped shell that
fails on import. Bubble's stance: the failure is observable at create
time, not deferred to the user's first invocation. The vault's
`packages` table has the answer to "is this triplet present?" — shell
creation consults it.

Sub-case 2 of issue #13.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble.run import shell as shell_mod
    from bubble.vault import db
    from bubble import host
    db.init_db()

    # One real package; one phantom name not in the vault.
    stage_fake_package(name="real-pkg", version="1.0.0", import_name="realpkg",
                       init_source='OK = True\n')

    # Free-form spec path — add() should record a missing entry.
    sd = shell_mod.create("orphantest", ["real-pkg", "phantom-pkg"])
    summary = shell_mod.add("orphantest", [])  # noop, just for shape
    manifest = shell_mod._read_manifest(sd)
    r.evidence.append(f"manifest after create: {sorted(manifest)}")

    if "phantom-pkg" in manifest:
        r.error = "phantom-pkg ended up in manifest despite not being in vault"
        return
    if "real-pkg" not in manifest:
        r.error = f"real-pkg should be in manifest; got {sorted(manifest)}"
        return

    # host.toml should record shell_pkg_missing for phantom-pkg
    failures = host.known_failures()
    phantom_records = [f for f in failures
                       if f.get("kind") == "shell_pkg_missing"
                       and "phantom-pkg" in (f.get("target", "") or f.get("detail", ""))]
    if not phantom_records:
        r.error = (
            f"shell_pkg_missing not recorded for phantom-pkg; "
            f"failures: {[(f.get('kind'), f.get('target')) for f in failures]}"
        )
        return

    r.evidence.append(f"phantom-pkg recorded as shell_pkg_missing ({len(phantom_records)} entries)")

    # add_pinned() with an exact triplet not in vault: same refusal shape.
    summary2 = shell_mod.add_pinned("orphantest", "phantom-pkg", "9.9.9", "py3-none-any")
    if not summary2.get("missing"):
        r.error = f"add_pinned should refuse phantom triplet; got {summary2}"
        return
    r.evidence.append(f"add_pinned refused phantom-pkg==9.9.9: {summary2['missing']}")

    r.passed = True


if __name__ == "__main__":
    run_test(
        "shell create / add_pinned refuses pins that aren't in the vault, "
        "with shell_pkg_missing recorded to host.toml — observable at "
        "create time, not deferred to first invocation",
        body,
    )
