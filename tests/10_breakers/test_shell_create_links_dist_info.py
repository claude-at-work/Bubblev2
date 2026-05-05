"""Claim: shell creation links *.dist-info into lib/, so importlib.metadata
sees the same set of distributions inside the shell that the vault has.

Without this, anything that uses entry-point metadata at runtime
(opentelemetry context loaders, pkg_resources plugins, click commands
declared as entry points, pytest plugins) silently has zero entries.

Sub-case 4 of issue #13.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble.run import shell as shell_mod
    from bubble.vault import db
    db.init_db()

    stage_fake_package(name="distinfopkg", version="3.1.4",
                       import_name="distinfopkg",
                       init_source='VERSION = "3.1.4"\n')

    sd = shell_mod.create("distinfotest", ["distinfopkg"])
    r.evidence.append(f"shell at {sd}")

    lib = sd / "lib"
    di_dir = lib / "distinfopkg-3.1.4.dist-info"
    if not di_dir.is_symlink():
        r.error = (
            f"dist-info not linked into shell lib: {di_dir} is not a symlink. "
            f"lib/ contains: {sorted(p.name for p in lib.iterdir())}"
        )
        return

    metadata = di_dir / "METADATA"
    if not metadata.is_file():
        r.error = f"METADATA not reachable through dist-info symlink: {metadata}"
        return

    r.evidence.append(f"dist-info symlinked: {di_dir.name}")
    r.evidence.append(f"METADATA reachable: {metadata.read_text().splitlines()[1]}")

    # Verify importlib.metadata sees the distribution. The reliable path
    # is to point sys.path at lib/ in a subprocess and call the API there —
    # avoids the parent process's caching/discovery state.
    import subprocess
    probe = (
        f"import sys; sys.path.insert(0, {str(lib)!r}); "
        "import importlib.metadata as md; "
        "d = md.distribution('distinfopkg'); "
        "print(d.name, d.version)"
    )
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        r.error = (f"importlib.metadata.distribution('distinfopkg') failed: "
                   f"{result.stderr.strip()}")
        return
    if "distinfopkg 3.1.4" not in result.stdout:
        r.error = f"distribution lookup wrong: {result.stdout!r}"
        return

    r.evidence.append(f"importlib.metadata.distribution: {result.stdout.strip()}")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "shell creation links *.dist-info dirs into lib/, so "
        "importlib.metadata.distribution() / .entry_points() inside the "
        "shell sees the same distributions the source vault has on disk — "
        "without this, entry-point-driven runtimes silently see nothing",
        body,
    )
