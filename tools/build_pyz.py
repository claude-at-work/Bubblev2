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
  - stdlib only (zipfile, hashlib, pathlib)
  - source tree is read, not executed
  - the produced .pyz is content-addressed: a sha256 over the bytes is
    written next to it, so the artifact carries its own integrity fact
  - the build is deterministic: same source bytes in → same archive
    bytes out → same sha256. Achieved by sorting entries and pinning
    every embedded mtime to a fixed epoch (SOURCE_DATE_EPOCH semantics
    drawn from the Reproducible Builds project, but with a default we
    set rather than borrow from environment to keep the artifact's
    fingerprint a function of source alone)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import stat
import struct
import sys
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUBBLE_PKG = REPO_ROOT / "bubble"

# Fixed epoch for embedded mtimes. 2020-01-01 00:00:00 UTC.
# Choice is arbitrary but stable: the artifact's sha256 must be a
# function of source bytes, not of when the build ran.
_FIXED_EPOCH = (2020, 1, 1, 0, 0, 0)

_IGNORE_NAMES = {"__pycache__"}
_IGNORE_SUFFIXES = {".pyc", ".pyo"}


def _walk_sources(root: Path) -> list[Path]:
    """Return all files under `root` that should be included in the
    archive, in deterministic (sorted, relative) order. Skips compiled
    artifacts and cache dirs so the archive is content-determined by
    source files alone."""
    files: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _IGNORE_NAMES for part in p.relative_to(root).parts):
            continue
        if p.suffix in _IGNORE_SUFFIXES:
            continue
        files.append(p)
    return files


def _write_archive(
    out_path: Path, sources: list[tuple[str, bytes]],
    interpreter: str, main_func: str,
) -> None:
    """Write a zipapp manually so every byte is under our control:
    sorted entries, fixed mtimes, fixed external attrs, no extras.

    The shebang line is written first as raw bytes, then the zip
    archive is appended. Python's zipimport reads from the end of the
    file and ignores any prefix, so the shebang doesn't disturb load."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # __main__.py inside the archive routes to the bubble entry
        # point. zipapp normally generates this; we generate it
        # ourselves with a deterministic body.
        module, func = main_func.split(":")
        main_py = (
            f"# -*- coding: utf-8 -*-\n"
            f"import {module}\n"
            f"{module}.{func}()\n"
        ).encode("utf-8")
        all_entries = [("__main__.py", main_py)] + sources
        all_entries.sort(key=lambda kv: kv[0])
        for arcname, data in all_entries:
            info = zipfile.ZipInfo(filename=arcname, date_time=_FIXED_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 << 16)  # -rw-r--r--
            info.create_system = 3  # Unix
            zf.writestr(info, data)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        f.write(b"#!" + interpreter.encode("utf-8") + b"\n")
        f.write(buf.getvalue())
    # Make the produced file executable so it can be invoked directly
    # (`./bubble.pyz vault list`). The shebang we just wrote is the
    # other half of that affordance.
    out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build(out_path: Path, *, interpreter: str = "/usr/bin/env python3") -> dict:
    out_path = out_path.resolve()
    src_files = _walk_sources(BUBBLE_PKG)
    sources: list[tuple[str, bytes]] = []
    for path in src_files:
        rel = "bubble/" + str(path.relative_to(BUBBLE_PKG)).replace("\\", "/")
        sources.append((rel, path.read_bytes()))

    _write_archive(out_path, sources, interpreter=interpreter,
                   main_func="bubble.cli:main")

    digest = _sha256(out_path)
    sidecar = out_path.with_suffix(out_path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {out_path.name}\n")
    return {
        "path": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "sha256": digest,
        "sidecar": str(sidecar),
        "file_count": len(sources) + 1,  # +1 for __main__.py
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build bubble.pyz (deterministic)")
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
    print(f"  size:    {info['size_bytes']} bytes")
    print(f"  files:   {info['file_count']}")
    print(f"  sha256:  {info['sha256']}")
    print(f"  sidecar: {info['sidecar']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
