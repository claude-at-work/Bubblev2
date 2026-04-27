"""Claim: vault-import refuses symlinked source files in a venv's RECORD.

Conventional intuition: hardlinking / copying with `follow_symlinks=False`
is fine — symlinks just become symlinks in the destination. Not for a
content-addressed vault: a symlink in the source means the bytes the
vault would serve under this name aren't the bytes the dist's RECORD
names. A malicious or compromised RECORD could enumerate
`evil_module.py → /etc/passwd` and place that symlink in our tree.

This test stages a fake site-packages where one RECORD entry is a symlink
pointing outside the dist. The importer must drop it on the floor.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble.vault import importer, db
    db.init_db()

    site_packages = Path(tempfile.mkdtemp(prefix="bubble-spkg-"))
    try:
        # Build a fake dist-info directory with a normal file + a symlink.
        dist_info = site_packages / "evil-1.0.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: evil\nVersion: 1.0.0\n\n"
        )
        (dist_info / "WHEEL").write_text(
            "Wheel-Version: 1.0\nTag: py3-none-any\n"
        )
        (dist_info / "top_level.txt").write_text("evil\n")

        evil_pkg = site_packages / "evil"
        evil_pkg.mkdir()
        (evil_pkg / "__init__.py").write_text("VERSION = '1.0.0'\n")

        # The trap: a symlinked module in the package, pointing outside.
        target_outside = Path(tempfile.mkdtemp(prefix="bubble-outside-")) / "secret.txt"
        target_outside.parent.mkdir(parents=True, exist_ok=True)
        target_outside.write_text("PRIVATE-CONTENT\n")
        os.symlink(target_outside, evil_pkg / "exfil.py")

        # RECORD lists the symlink as if it were just another module file.
        (dist_info / "RECORD").write_text(
            "evil/__init__.py,sha256=,1\n"
            "evil/exfil.py,sha256=,1\n"
            "evil-1.0.0.dist-info/METADATA,,\n"
            "evil-1.0.0.dist-info/WHEEL,,\n"
            "evil-1.0.0.dist-info/top_level.txt,,\n"
        )

        result = importer.import_dist_info(dist_info)
        assert result is not None, "import_dist_info returned None"
        name, version, tag, copied = result
        assert (name, version, tag) == ("evil", "1.0.0", "py3-none-any")

        # The symlinked file must NOT have made it into the vault.
        from bubble import config
        vault_pkg = config.VAULT_DIR / "evil" / "1.0.0" / "py3-none-any" / "evil"
        landed = sorted(p.name for p in vault_pkg.iterdir())
        assert "exfil.py" not in landed, \
            f"symlinked exfil.py landed in vault: {landed}"
        assert "__init__.py" in landed, \
            f"legitimate __init__.py missing: {landed}"
        r.evidence.append(f"vault contents under evil/: {landed}")
        r.evidence.append(
            "symlink RECORD entry refused; non-symlinked modules pass through"
        )

        # And the secret stays secret.
        for p in vault_pkg.iterdir():
            try:
                content = p.read_text()
            except OSError:
                continue
            assert "PRIVATE-CONTENT" not in content, \
                f"vault content contains the symlink target: {p}"
        r.evidence.append("symlink target's bytes never reached the vault")
    finally:
        shutil.rmtree(site_packages, ignore_errors=True)
    r.passed = True


if __name__ == "__main__":
    run_test(
        "vault import-venv refuses symlinked RECORD entries: the bytes "
        "a content-addressed vault serves under a name must come from the "
        "file the dist's RECORD names, not from wherever a symlink chain "
        "happens to terminate",
        body,
    )
