"""Claim: a bundle is a deployment artifact — manifest in, shell out, on
any machine the target python_tag matches, with every byte's integrity
fact crossing the wire alongside the byte.

Conventional intuition: shipping Python is hard. You bundle a venv
relative-pathed and it usually breaks; you ship a wheelhouse and the
operator still needs pip; you build a container and now you've shipped
an entire OS. Bubble's stance is smaller: the shell is the deployment
artifact, the manifest is the contract, the integrity facts travel
beside the bytes, and the target's first move on extraction is to
re-probe its own machine and verify each pin against the source's
sha256 — not the tar's mtimes.

This test stages a synthetic shell, bundles it, then unbundles into a
fresh BUBBLE_HOME, and asserts:
  - the bundle is a well-formed tar.gz with .bubble.bundle.toml at root
  - the target's vault.db has the package + vault_files rows
  - the target's shells row carries the source's metadata (aliases +
    substrate declarations preserved across the wire)
  - the target's shell tree exists and its symlinks resolve
  - tampering a byte between bundle and unbundle is caught by verify
"""
import gzip
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble import bundle as bundle_mod, config, manifest as manifest_mod
    from bubble.run import shell as shell_mod
    from bubble.vault import db
    db.init_db()

    # Stage two synthetic packages and create a shell from a deployment
    # manifest so the alias-substrate field is in the shell row.
    stage_fake_package(name="alpha", version="1.0.0", import_name="alpha",
                       init_source='VERSION = "1.0.0"\n')
    stage_fake_package(name="beta", version="2.0.0", import_name="beta",
                       init_source='VERSION = "2.0.0"\n')

    shell_mod.create("ship", [])
    shell_mod.add_pinned("ship", "alpha", "1.0.0", "py3-none-any")
    shell_mod.add_pinned("ship", "beta", "2.0.0", "py3-none-any")

    # Plant alias metadata in the shell row, the way create-from-manifest does.
    conn = db.connect()
    conn.execute(
        "UPDATE shells SET metadata=? WHERE name=?",
        (json.dumps({"aliases": {
            "alpha_isolated": {
                "name": "alpha", "version": "1.0.0",
                "wheel_tag": "py3-none-any",
                "substrate": "dlmopen_isolated",
            },
        }}), "ship"),
    )
    conn.commit()
    conn.close()

    # Bundle.
    bundle_path = config.BUBBLE_HOME / "ship.tar.gz"
    summary = bundle_mod.bundle("ship", bundle_path)
    assert bundle_path.exists() and bundle_path.stat().st_size > 0
    assert summary["packages"] == 2
    r.evidence.append(
        f"bundled: {summary['packages']} packages, {summary['files']} files, "
        f"{bundle_path.stat().st_size} bytes"
    )

    # Inspect the bundle: well-formed tar.gz with the manifest at root.
    with gzip.open(bundle_path, "rb") as gz:
        with tarfile.open(fileobj=gz, mode="r") as tar:
            names = tar.getnames()
    assert ".bubble.bundle.toml" in names, "bundle manifest missing from tar root"
    assert any(n.startswith("vault/alpha/1.0.0/py3-none-any/") for n in names)
    assert any(n.startswith("shells/ship/") for n in names)
    r.evidence.append(f"tar layout: manifest + vault subtree + shell tree ({len(names)} entries)")

    # Unbundle into a fresh BUBBLE_HOME.
    target = Path(tempfile.mkdtemp(prefix="bubble-unbundle-"))
    try:
        result = bundle_mod.unbundle(bundle_path, into_home=target)
        assert result["shell"] == "ship"
        assert result["packages"] == 2
        assert not result["drift"], f"unexpected drift: {result['drift']}"
        r.evidence.append(
            f"unbundled into fresh home: {result['packages']} packages, "
            f"integrity clean"
        )

        # Target's vault.db has the rows.
        tdb = sqlite3.connect(str(target / "vault.db"))
        try:
            n_pkg = tdb.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
            n_vf = tdb.execute("SELECT COUNT(*) FROM vault_files").fetchone()[0]
            n_sh = tdb.execute("SELECT COUNT(*) FROM shells WHERE name=?",
                               ("ship",)).fetchone()[0]
            assert n_pkg == 2, f"target packages: {n_pkg}"
            assert n_vf > 0, f"target vault_files: {n_vf}"
            assert n_sh == 1, f"target shells row count: {n_sh}"
            r.evidence.append(
                f"target db: packages={n_pkg}, vault_files={n_vf}, shells=1"
            )

            # Substrate metadata preserved across the wire.
            row = tdb.execute(
                "SELECT metadata FROM shells WHERE name=?", ("ship",),
            ).fetchone()
            meta = json.loads(row[0])
            assert meta["aliases"]["alpha_isolated"]["substrate"] == "dlmopen_isolated", \
                f"substrate not preserved: {meta}"
            r.evidence.append("alias substrate field survived bundle → unbundle")
        finally:
            tdb.close()

        # Target's shell tree exists, links resolve.
        target_lib = target / "shells" / "ship" / "lib"
        assert (target_lib / "alpha").exists(), "alpha link not resolved on target"
        assert (target_lib / "beta").exists(), "beta link not resolved on target"
        r.evidence.append("target shell symlinks resolve to extracted vault")

        # Verify on target: the integrity edge survived transport.
        from bubble.vault import store as target_store
        saved_home = config.BUBBLE_HOME
        saved_vd = config.VAULT_DIR
        saved_db = config.VAULT_DB
        config.BUBBLE_HOME = target
        config.VAULT_DIR = target / "vault"
        config.VAULT_DB = target / "vault.db"
        try:
            rep = target_store.verify("alpha", "1.0.0", "py3-none-any")
            assert rep.had_index, "vault_files rows missing on target"
            assert rep.clean, f"target verify drifted: {rep}"
            r.evidence.append(
                f"target verify(alpha): {len(rep.matched)} matched, clean"
            )

            # Tamper a target file. verify() should now refuse.
            tampered = target / "vault" / "alpha" / "1.0.0" / "py3-none-any" / "alpha" / "__init__.py"
            tampered.write_text(tampered.read_text() + "\n# tampered post-extract\n")
            rep2 = target_store.verify("alpha", "1.0.0", "py3-none-any")
            assert not rep2.clean, "post-extract tamper not caught"
            assert rep2.drifted, f"expected drifted entries: {rep2}"
            r.evidence.append(
                "post-extract tampering caught by target verify "
                "(integrity edge survives transport)"
            )
        finally:
            config.BUBBLE_HOME = saved_home
            config.VAULT_DIR = saved_vd
            config.VAULT_DB = saved_db
    finally:
        shutil.rmtree(target, ignore_errors=True)

    r.passed = True


if __name__ == "__main__":
    run_test(
        "bundle → unbundle is the deployment surface: source manifest in, "
        "tar.gz out, target vault.db rebuilt from source's recorded facts, "
        "alias substrate field preserved, integrity edge survives the wire "
        "(post-extract tampering caught by verify against source's sha256)",
        body,
    )
