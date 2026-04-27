"""Import already-installed packages from a venv site-packages into the vault.

Reads each *.dist-info, parses METADATA + WHEEL to discover (name, version,
wheel_tag), then copies all files listed in RECORD into a staging dir, then
commits to the vault.
"""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path
from typing import Iterator, Optional

from . import db, store, metadata as meta


def _record_paths(dist_info: Path) -> Iterator[Path]:
    """Yield relative paths (relative to site-packages) listed in RECORD."""
    record_file = dist_info / "RECORD"
    if not record_file.exists():
        return
    site_packages = dist_info.parent
    with record_file.open(newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row:
                continue
            rel = row[0]
            if not rel:
                continue
            # Skip absolute paths (rare) and parent traversal
            if rel.startswith("/") or ".." in Path(rel).parts:
                continue
            yield Path(rel)


def _copy_into_stage(
    site_packages: Path,
    rel_paths: list[Path],
    staged: Path,
    *,
    hardlink: bool = False,
) -> int:
    """Copy or hardlink each rel_path from site-packages into staged.

    Sovereignty guardrails:
      - Refuse symlinks anywhere on the source path. A symlinked source
        (or a symlinked parent component) means the file we'd vault isn't
        the file the dist's RECORD names — anything from `/etc/passwd` to
        a sibling-venv binary could be aliased in.
      - Resolve the destination and verify it stays under `staged`.
        Belt-and-braces: rel_paths are already filtered for `..`, but a
        crafted RECORD could contain symlinks that escape on creation.
    """
    staged_resolved = staged.resolve()
    n = 0
    for rel in rel_paths:
        src = site_packages / rel
        # is_symlink is the leaf check; lstat the chain of parents to catch
        # symlinks higher up that would change what `src` actually resolves to.
        if src.is_symlink():
            continue
        skip = False
        cur = src.parent
        while cur != site_packages and cur != cur.parent:
            if cur.is_symlink():
                skip = True
                break
            cur = cur.parent
        if skip:
            continue
        if not src.exists() or src.is_dir():
            continue
        dst = staged / rel
        try:
            dst_resolved = (staged / rel).resolve()
        except OSError:
            continue
        if staged_resolved != dst_resolved and staged_resolved not in dst_resolved.parents:
            # Destination escapes staged dir — drop on the floor.
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if hardlink:
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst, follow_symlinks=False)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
            n += 1
        except (OSError, shutil.Error):
            # missing/unreadable file in record → skip
            continue
    return n


def import_dist_info(
    dist_info: Path,
    *,
    hardlink: bool = False,
    overwrite: bool = False,
) -> Optional[tuple[str, str, str, int]]:
    """Import one *.dist-info package into the vault.

    Returns (name, version, wheel_tag, files_copied) on success, None on skip.
    """
    if not dist_info.is_dir() or not dist_info.name.endswith(".dist-info"):
        return None

    nv = meta.name_version_from_dist_info(dist_info)
    if not nv:
        return None
    name, version = nv
    wheel_tag, py_tag, abi_tag, plat_tag = meta.derive_wheel_tag_from_dist_info(dist_info)

    conn = db.connect()
    if not overwrite and store.has(conn, name, version, wheel_tag):
        conn.close()
        return None
    conn.close()

    site_packages = dist_info.parent
    rel_paths = list(_record_paths(dist_info))
    if not rel_paths:
        return None

    staged = store.stage_dir()
    try:
        copied = _copy_into_stage(site_packages, rel_paths, staged, hardlink=hardlink)
        if copied == 0:
            shutil.rmtree(staged, ignore_errors=True)
            return None
        # Pull a few interesting metadata bits for the JSON column
        md_text = (dist_info / "METADATA").read_text(errors="replace")
        md = meta.parse_metadata(md_text)
        metadata_blob = {
            "summary": md.get("summary"),
            "requires_python": md.get("requires_python"),
            "requires_dist": md.get("requires_dist", []),
            "imported_from": str(site_packages),
        }
        store.commit(
            name=name,
            version=version,
            wheel_tag=wheel_tag,
            python_tag=py_tag,
            abi_tag=abi_tag,
            platform_tag=plat_tag,
            staged=staged,
            source="venv-import",
            sha256=None,
            metadata=metadata_blob,
            overwrite=overwrite,
        )
        return (name, version, wheel_tag, copied)
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise


def import_site_packages(
    site_packages: Path,
    *,
    hardlink: bool = False,
    overwrite: bool = False,
    skip: Optional[set[str]] = None,
) -> dict:
    """Import every *.dist-info in a site-packages dir.

    Returns summary dict: {imported, skipped, missing_record, errors,
                            entries: [(name, version, wheel_tag, files), ...]}.
    """
    if skip is None:
        skip = set()
    site_packages = Path(site_packages)
    if not site_packages.is_dir():
        raise ValueError(f"not a directory: {site_packages}")

    summary = {
        "imported": 0,
        "skipped": 0,
        "missing_record": 0,
        "errors": 0,
        "entries": [],
        "skipped_names": [],
    }
    for di in sorted(site_packages.glob("*.dist-info")):
        nv = meta.name_version_from_dist_info(di)
        if nv and meta.normalize_name(nv[0]) in skip:
            summary["skipped"] += 1
            summary["skipped_names"].append(nv[0])
            continue
        try:
            result = import_dist_info(di, hardlink=hardlink, overwrite=overwrite)
        except Exception as exc:
            summary["errors"] += 1
            summary["entries"].append((di.name, "ERROR", str(exc), 0))
            continue
        if result is None:
            # Either already vaulted or missing RECORD
            if not (di / "RECORD").exists():
                summary["missing_record"] += 1
            else:
                summary["skipped"] += 1
                if nv:
                    summary["skipped_names"].append(nv[0])
            continue
        summary["imported"] += 1
        summary["entries"].append(result)
    return summary
