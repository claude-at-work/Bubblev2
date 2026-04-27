"""Build bubble.pyz from the `bubble/` source tree.

Path B of the bootstrap thread: bubble produces its own deployment
artifact via stdlib `zipapp`, end to end. The script is itself a
script bubble can `run` — `python3 -m bubble run tools/build_pyz.py`
walks the recursive self-host one notch.

The discipline this script does NOT cross: it does not run any
installation script for any third-party package. Bubble has no
third-party deps, so there's nothing to install; the build is a pure
copy-and-zip operation. That's the whole point — the safe-side bootstrap
demonstrates the existing line without testing it.

Invariants preserved:
  - stdlib only (zipapp, shutil, pathlib, hashlib)
  - source tree is read, not executed
  - the produced .pyz is content-addressed: a sha256 over the bytes is
    written next to it, so the artifact carries its own integrity fact
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUBBLE_PKG = REPO_ROOT / "bubble"


def _stage(staging: Path) -> Path:
    """Copy bubble/ into a staging dir. zipapp wants a directory whose
    contents become the archive root; we put bubble/ at the root so
    `python bubble.pyz` finds bubble/__main__.py."""
    target = staging / "bubble"
    shutil.copytree(
        BUBBLE_PKG, target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    return staging


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build(out_path: Path, *, interpreter: str = "/usr/bin/env python3") -> dict:
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bubble-pyz-build-") as td:
        staging = _stage(Path(td))
        zipapp.create_archive(
            source=staging,
            target=out_path,
            interpreter=interpreter,
            main="bubble.cli:main",
            compressed=True,
        )

    digest = _sha256(out_path)
    sidecar = out_path.with_suffix(out_path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {out_path.name}\n")
    return {
        "path": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "sha256": digest,
        "sidecar": str(sidecar),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build bubble.pyz")
    p.add_argument(
        "-o", "--output", default=str(REPO_ROOT / "bubble.pyz"),
        help="output path (default: bubble.pyz at repo root)",
    )
    p.add_argument(
        "--interpreter", default="/usr/bin/env python3",
        help="shebang interpreter line for the zipapp",
    )
    args = p.parse_args(argv)

    info = build(Path(args.output), interpreter=args.interpreter)
    print(f"built {info['path']}")
    print(f"  size:   {info['size_bytes']} bytes")
    print(f"  sha256: {info['sha256']}")
    print(f"  sidecar: {info['sidecar']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
