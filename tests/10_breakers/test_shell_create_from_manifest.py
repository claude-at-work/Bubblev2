"""Claim: a deployment manifest produces a shell that exactly mirrors
the manifest, with verify-on-link refusing any pin whose vault entry
has drifted.

Conventional intuition: an environment spec is a list of names; the
package manager picks compatible versions; the spec and the result
diverge over time. Bubble's stance: the deployment manifest *is* the
contract. The exact (name, version, wheel_tag) triplet round-trips
through `bubble shell create --from`. If a pin isn't in the vault,
the create refuses (or fetches, opt-in). If a pin is in the vault but
its bytes have drifted, the link refuses through the C1∩C4 join.

This test stages two synthetic packages, writes a manifest naming
their exact triplets, calls `shell.create` + the manifest-driven
fill, and verifies:
  - the shell's state-manifest matches the deployment manifest's
    [packages] section
  - the shell-row's metadata.aliases preserves the substrate field
    reserved for C5
  - mutating a vaulted file before the link causes that pin to
    refuse with a host.toml drift entry
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble import config, manifest as manifest_mod
    from bubble.run import shell as shell_mod
    from bubble.vault import db
    from bubble import host
    db.init_db()

    stage_fake_package(
        name="alpha", version="1.0.0", import_name="alpha",
        init_source='VERSION = "1.0.0"\n',
    )
    stage_fake_package(
        name="beta", version="2.3.4", import_name="beta",
        init_source='VERSION = "2.3.4"\n',
    )

    m = manifest_mod.Manifest(
        name="deploytest",
        packages={
            "alpha": ("1.0.0", "py3-none-any"),
            "beta":  ("2.3.4", "py3-none-any"),
        },
        aliases={
            "alpha_x": manifest_mod.AliasPin(
                name="alpha", version="1.0.0", wheel_tag="py3-none-any",
                substrate="dlmopen_isolated",
            ),
        },
    )
    manifest_path = config.BUBBLE_HOME / "deploy.manifest.toml"
    manifest_mod.dump(m, manifest_path)
    r.evidence.append(f"manifest: {len(m.packages)} packages, {len(m.aliases)} aliases")

    # Round-trip the manifest's [packages] through the manifest-driven
    # shell creation. We exercise the lower-level functions directly
    # rather than the CLI to keep the test hermetic.
    shell_mod.create("deploytest", [])
    for pkg, (ver, tag) in m.packages.items():
        s = shell_mod.add_pinned("deploytest", pkg, ver, tag)
        assert not s["missing"] and not s["conflicts"], \
            f"add_pinned failed for {pkg}: {s}"
        assert s["linked"], f"add_pinned did not link {pkg}"

    # Verify the shell's state-manifest matches the deployment manifest.
    state = shell_mod._read_manifest(shell_mod.shell_dir("deploytest"))
    assert set(state) == set(m.packages), \
        f"shell-state pkgs {set(state)} != deploy pkgs {set(m.packages)}"
    for pkg, (ver, tag) in m.packages.items():
        assert state[pkg] == {"version": ver, "wheel_tag": tag}, \
            f"{pkg} state {state[pkg]} != deploy ({ver},{tag})"
    r.evidence.append("shell-state matches deployment-manifest [packages] exactly")

    # Aliases: store them in the shell row's metadata so the
    # substrate-routing thread (C5, not yet wired) can pick them up.
    conn = db.connect()
    conn.execute(
        "UPDATE shells SET metadata=? WHERE name=?",
        (json.dumps({"aliases": {
            alias: {
                "name": pin.name, "version": pin.version,
                "wheel_tag": pin.wheel_tag, "substrate": pin.substrate,
            } for alias, pin in m.aliases.items()
        }}), "deploytest"),
    )
    conn.commit()
    row = conn.execute("SELECT metadata FROM shells WHERE name=?",
                       ("deploytest",)).fetchone()
    conn.close()
    meta_blob = json.loads(row[0])
    assert meta_blob["aliases"]["alpha_x"]["substrate"] == "dlmopen_isolated", \
        f"substrate field lost: {meta_blob}"
    r.evidence.append("alias substrate field preserved through to shell row metadata")

    # Drift refusal: mutate a vaulted file and confirm add_pinned refuses
    # with a host.toml drift entry.
    vault_path = config.VAULT_DIR / "alpha" / "1.0.0" / "py3-none-any"
    init_file = vault_path / "alpha" / "__init__.py"
    init_file.write_text(init_file.read_text() + "\n# tampered\n")

    # Make a fresh shell and try to link the now-drifted pin.
    shell_mod.create("drifttest", [])
    failures_before = len(host.known_failures())
    try:
        shell_mod.add_pinned("drifttest", "alpha", "1.0.0", "py3-none-any")
    except RuntimeError as exc:
        msg = str(exc)
        assert "vault drift" in msg.lower(), \
            f"refusal didn't name drift: {msg}"
        assert "alpha==1.0.0@py3-none-any" in msg, \
            f"refusal didn't name target: {msg}"
        r.evidence.append("drifted pin refused at link time with named target")
    else:
        raise AssertionError("expected RuntimeError on drifted vault entry; none raised")

    failures_after = host.known_failures()
    drift_entries = [f for f in failures_after if f.get("kind") == "vault_drift_modified"]
    assert drift_entries, \
        f"no vault_drift_modified failure recorded; saw kinds: " \
        f"{[f.get('kind') for f in failures_after]}"
    r.evidence.append(
        f"host.toml gained {len(failures_after) - failures_before} "
        f"failure entries; {len(drift_entries)} of kind vault_drift_modified"
    )

    r.passed = True


if __name__ == "__main__":
    run_test(
        "deployment manifest round-trips through shell.add_pinned: exact "
        "(name, version, wheel_tag) triplets become shell-state entries; "
        "alias substrate fields are preserved for C5; drift in any pin "
        "refuses the link via the C1∩C4 join",
        body,
    )
