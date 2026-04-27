"""Bundle a shell into a portable artifact; unbundle on a target machine.

A bundle is a tar.gz containing:
  - .bubble.bundle.toml          — bundle manifest (deployment manifest +
                                    integrity facts + source-machine portrait)
  - vault/<pkg>/<ver>/<tag>/...  — the vault subset the shell's closure pins
  - shells/<name>/...            — the shell tree itself

Relocatability: every symlink in the shell tree was emitted relative to
its container at create time, so the tar's content is path-independent.
The receiving machine extracts to its own BUBBLE_HOME and the links
resolve against the new tree without rewriting.

Trust chain: the bundle manifest carries the vault_files rows from the
source machine — sha256 + size for every byte in every vaulted file in
the closure. On unbundle, the target writes those rows into its own
vault.db before re-statting; subsequent calls to `store.verify()` then
work against the source's cryptographic facts, not the tar's mtimes.
The integrity edge survives the transport.

The probe is NOT serialized into the bundle. Each target probes itself
on unbundle; the source's host.toml is included separately as
`source_host.toml` for inspection (and, in a future thread, comparison).
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, manifest as manifest_mod
from .vault import db, store, metadata as meta


BUNDLE_MANIFEST_NAME = ".bubble.bundle.toml"


# ─────────────────────── bundle creation ────────────────────────


def bundle(shell_name: str, output_path: Path) -> dict:
    """Bundle a shell into a tar.gz at `output_path`.

    Returns a small summary dict: {packages, files, bytes}. Raises
    FileNotFoundError if the shell doesn't exist.
    """
    from .run.shell import shell_dir, _read_manifest as _read_shell_manifest
    sd = shell_dir(shell_name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {shell_name}")

    state = _read_shell_manifest(sd)
    if not state:
        raise ValueError(f"shell {shell_name} has empty manifest — nothing to bundle")

    # Build the deployment manifest from the shell's state. Aliases live
    # in the shell row's metadata blob (see cli._shell_create_from_manifest);
    # carry them through so a roundtrip preserves substrate declarations.
    deploy = manifest_mod.Manifest(name=shell_name)
    for pkg, info in state.items():
        deploy.packages[pkg] = (info["version"], info["wheel_tag"])

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT metadata FROM shells WHERE name=?", (shell_name,),
        ).fetchone()
        if row and row[0]:
            try:
                meta_blob = json.loads(row[0])
                for alias_name, pin in (meta_blob.get("aliases") or {}).items():
                    deploy.aliases[alias_name] = manifest_mod.AliasPin(
                        name=pin["name"], version=pin["version"],
                        wheel_tag=pin["wheel_tag"],
                        substrate=pin.get("substrate"),
                    )
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # Collect packages + vault_files rows for the closure.
        pkg_rows = []
        vf_rows = []
        for pkg, (ver, tag) in deploy.packages.items():
            prow = conn.execute(
                "SELECT name, version, wheel_tag, python_tag, abi_tag, "
                "platform_tag, sha256, source, has_native, metadata "
                "FROM packages WHERE name=? AND version=? AND wheel_tag=?",
                (pkg, ver, tag),
            ).fetchone()
            if not prow:
                raise RuntimeError(
                    f"shell pin {pkg}=={ver}@{tag} not in vault — "
                    f"cannot bundle a closure with missing entries"
                )
            pkg_rows.append(prow)
            for vrow in conn.execute(
                "SELECT rel_path, sha256, size_bytes FROM vault_files "
                "WHERE package=? AND version=? AND wheel_tag=?",
                (pkg, ver, tag),
            ):
                vf_rows.append((pkg, ver, tag, *vrow))
    finally:
        conn.close()

    # Capture the source shell row's full metadata blob so the target
    # can rebuild the row identically (alias substrate declarations live
    # here; without this the deploy manifest's aliases land on the
    # target's filesystem but not in its `shells` row).
    shell_meta_json = "{}"
    if row and row[0]:
        shell_meta_json = row[0]

    bundle_manifest = _emit_bundle_manifest(
        shell_name=shell_name,
        deploy=deploy,
        pkg_rows=pkg_rows,
        vf_rows=vf_rows,
        shell_metadata_json=shell_meta_json,
    )

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {"packages": len(deploy.packages), "files": 0, "bytes": 0}
    with gzip.open(output_path, "wb") as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            # 1. The bundle manifest at root.
            data = bundle_manifest.encode("utf-8")
            info = tarfile.TarInfo(name=BUNDLE_MANIFEST_NAME)
            info.size = len(data)
            info.mode = 0o600
            info.mtime = int(datetime.now().timestamp())
            tar.addfile(info, io.BytesIO(data))

            # 2. The vault subset for this shell's closure.
            for pkg, (ver, tag) in deploy.packages.items():
                vault_path = store.vault_path_for(pkg, ver, tag)
                if not vault_path.exists():
                    continue
                arc_root = f"vault/{pkg}/{ver}/{tag}"
                _add_tree_to_tar(tar, vault_path, arc_root, summary)

            # 3. The shell tree (relative symlinks travel as-is).
            shell_arc_root = f"shells/{shell_name}"
            _add_tree_to_tar(tar, sd, shell_arc_root, summary)

            # 4. The source machine's host.toml (informational, not
            # used for integrity — included so the target can inspect
            # what worked on the source machine).
            src_host = config.BUBBLE_HOME / "host.toml"
            if src_host.exists():
                info = tar.gettarinfo(str(src_host),
                                      arcname="source_host.toml")
                with src_host.open("rb") as fh:
                    tar.addfile(info, fh)

    summary["bytes"] = output_path.stat().st_size
    return summary


def _add_tree_to_tar(tar: tarfile.TarFile, root: Path,
                     arc_root: str, summary: dict) -> None:
    """Add `root` and everything under it to `tar` under `arc_root`,
    preserving relative symlinks verbatim. We don't follow symlinks —
    the link itself is what carries the structure."""
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        arcname = f"{arc_root}/{rel}"
        if path.is_symlink():
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.SYMTYPE
            info.linkname = str(Path(arcname).parent / "_") + ""  # placeholder
            # Read the actual link target — relative or absolute as written.
            import os as _os
            info.linkname = _os.readlink(str(path))
            info.mode = 0o777
            tar.addfile(info)
            summary["files"] += 1
        elif path.is_file():
            info = tar.gettarinfo(str(path), arcname=arcname)
            with path.open("rb") as fh:
                tar.addfile(info, fh)
            summary["files"] += 1
        elif path.is_dir():
            info = tarfile.TarInfo(name=arcname)
            info.type = tarfile.DIRTYPE
            info.mode = 0o700
            tar.addfile(info)


def _emit_bundle_manifest(*, shell_name: str,
                          deploy: manifest_mod.Manifest,
                          pkg_rows: list,
                          vf_rows: list,
                          shell_metadata_json: str = "{}") -> str:
    """The bundle manifest is a TOML document the unbundle path reads
    to rebuild vault.db rows on the target machine."""
    lines: list[str] = [
        "# bubble bundle manifest",
        f'name = "{shell_name}"',
        f'created_at = "{datetime.now().isoformat()}"',
        f'bubble_version = "0.3.0"',
        f'shell_metadata_json = "{_e(shell_metadata_json)}"',
        "",
        "[source_host]",
        f'python_tag = "{config.runner_python_tag()}"',
        f'platform_tag = "{config.runner_platform_tag()}"',
        f'host = "{config.detect_host()}"',
        "",
    ]
    if deploy.packages:
        lines.append("[packages]")
        for pkg in sorted(deploy.packages):
            ver, tag = deploy.packages[pkg]
            lines.append(
                f'"{_e(pkg)}" = {{ '
                f'version = "{_e(ver)}", '
                f'wheel_tag = "{_e(tag)}" }}'
            )
        lines.append("")
    if deploy.aliases:
        lines.append("[aliases]")
        for alias in sorted(deploy.aliases):
            pin = deploy.aliases[alias]
            parts = [
                f'name = "{_e(pin.name)}"',
                f'version = "{_e(pin.version)}"',
                f'wheel_tag = "{_e(pin.wheel_tag)}"',
            ]
            if pin.substrate:
                parts.append(f'substrate = "{_e(pin.substrate)}"')
            lines.append(f"{alias} = {{ " + ", ".join(parts) + " }")
        lines.append("")

    # Each package's full row + its file integrity facts. This is what
    # the target uses to rebuild vault.db after extraction.
    for prow in pkg_rows:
        (pname, pver, ptag, py, abi, plat, psha,
         psource, has_native, pmd) = prow
        lines.append("[[bundled_packages]]")
        lines.append(f'name = "{_e(pname)}"')
        lines.append(f'version = "{_e(pver)}"')
        lines.append(f'wheel_tag = "{_e(ptag)}"')
        lines.append(f'python_tag = "{_e(py or "")}"')
        lines.append(f'abi_tag = "{_e(abi or "")}"')
        lines.append(f'platform_tag = "{_e(plat or "")}"')
        lines.append(f'sha256 = "{_e(psha or "")}"')
        lines.append(f'source = "{_e(psource or "")}"')
        lines.append(f"has_native = {1 if has_native else 0}")
        lines.append(f'metadata_json = "{_e(pmd or "{}")}"')
        lines.append("")

    for pkg, ver, tag, rel, sha, size in vf_rows:
        lines.append("[[bundled_files]]")
        lines.append(f'package = "{_e(pkg)}"')
        lines.append(f'version = "{_e(ver)}"')
        lines.append(f'wheel_tag = "{_e(tag)}"')
        lines.append(f'rel_path = "{_e(rel)}"')
        lines.append(f'sha256 = "{_e(sha)}"')
        lines.append(f"size_bytes = {size}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _e(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ─────────────────────── unbundling ─────────────────────────────


def unbundle(tar_path: Path, into_home: Optional[Path] = None,
             *, allow_python_mismatch: bool = False) -> dict:
    """Extract a bundle into BUBBLE_HOME (or `into_home`), rebuild
    vault.db rows, run probe, verify each extracted entry.

    Returns a summary: {shell, packages, drift, source_python, target_python}.
    """
    tar_path = Path(tar_path).resolve()
    if not tar_path.exists():
        raise FileNotFoundError(f"bundle not found: {tar_path}")

    target_home = Path(into_home).resolve() if into_home else config.BUBBLE_HOME

    # Read the bundle manifest first (without extracting the tree) so we
    # can refuse a python_tag mismatch *before* touching the target's
    # filesystem.
    with gzip.open(tar_path, "rb") as gz:
        with tarfile.open(fileobj=gz, mode="r") as tar:
            try:
                m_member = tar.getmember(BUNDLE_MANIFEST_NAME)
            except KeyError:
                raise ValueError(
                    f"{tar_path} is not a bubble bundle "
                    f"(missing {BUNDLE_MANIFEST_NAME})"
                )
            m_fh = tar.extractfile(m_member)
            if m_fh is None:
                raise ValueError(f"{tar_path} bundle manifest unreadable")
            manifest_text = m_fh.read().decode("utf-8")

    bundle_meta = _parse_bundle_manifest(manifest_text)
    src_py = bundle_meta["source_host"].get("python_tag", "")
    tgt_py = config.runner_python_tag()
    if src_py and src_py != tgt_py and not allow_python_mismatch:
        raise RuntimeError(
            f"bundle was built for python_tag={src_py!r} but this target "
            f"is {tgt_py!r}. Wheel-tagged packages will not be ABI-compatible. "
            f"Re-bundle on the source against the target's python, or pass "
            f"--allow-python-mismatch to extract anyway (the verify step "
            f"will likely refuse most pins)."
        )

    # Now extract everything except symlinks via the data filter; replay
    # symlinks afterwards so we can verify each link target stays inside
    # the bundle root.
    target_home.mkdir(parents=True, exist_ok=True)
    target_home.chmod(0o700)
    with gzip.open(tar_path, "rb") as gz:
        with tarfile.open(fileobj=gz, mode="r") as tar:
            for member in tar.getmembers():
                if member.name == BUNDLE_MANIFEST_NAME:
                    continue  # don't write the manifest into BUBBLE_HOME
                if member.name == "source_host.toml":
                    # write as ./bundles/<name>/source_host.toml — won't
                    # collide with the target's own host.toml.
                    src_dest = target_home / "bundles" / bundle_meta["name"] / "source_host.toml"
                    src_dest.parent.mkdir(parents=True, exist_ok=True)
                    fh = tar.extractfile(member)
                    if fh is not None:
                        src_dest.write_bytes(fh.read())
                    continue
                _safe_extract_member(tar, member, target_home)

    # Reinitialize the target's vault.db (or merge into existing) using
    # the bundle's recorded rows. Now `verify()` on the target consults
    # the source machine's cryptographic facts.
    _merge_db_rows_from_bundle(bundle_meta, target_home)

    # Update vault_files mtime_ns to actual on-disk values — the sha is
    # the source of truth, mtime is just the fast-path key.
    _refresh_mtimes_after_extract(bundle_meta, target_home)

    # Run probe on the target to write a fresh host.toml. The source's
    # host.toml is preserved separately for inspection; the live
    # host.toml is the target's, the only source of truth for
    # substrate-availability decisions.
    from . import probe
    saved_home = config.BUBBLE_HOME
    config.BUBBLE_HOME = target_home
    config.VAULT_DIR = target_home / "vault"
    config.VAULT_DB = target_home / "vault.db"
    config.STAGING_DIR = config.VAULT_DIR / ".staging"
    try:
        probe_results = probe.run_all()
        probe.write(probe.host_toml_path(), probe_results)

        # Verify each unbundled package against its bundled vault_files
        # rows. Drift = corruption in transit. Refuse to expose the
        # shell if any pin drifted.
        drift_packages: list[str] = []
        for pkg, ver, tag in [
            (b["name"], b["version"], b["wheel_tag"])
            for b in bundle_meta["bundled_packages"]
        ]:
            report = store.verify(pkg, ver, tag)
            if report.had_index and not report.clean:
                drift_packages.append(f"{pkg}=={ver}@{tag}")
                from . import host
                target = f"{pkg}=={ver}@{tag}"
                for rel, kind in report.drifted:
                    host.record_failure(kind, target, f"rel={rel}; from bundle")
                for rel in report.missing:
                    host.record_failure("vault_drift_missing", target,
                                        f"rel={rel}; from bundle")
    finally:
        config.BUBBLE_HOME = saved_home
        config.VAULT_DIR = saved_home / "vault"
        config.VAULT_DB = saved_home / "vault.db"
        config.STAGING_DIR = config.VAULT_DIR / ".staging"

    return {
        "shell": bundle_meta["name"],
        "packages": len(bundle_meta["bundled_packages"]),
        "drift": drift_packages,
        "source_python": src_py,
        "target_python": tgt_py,
        "into_home": str(target_home),
    }


def _safe_extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo,
                         target_home: Path) -> None:
    """Extract one member with the same protections as the wheel
    extractor: reject absolute paths, parent traversal, null bytes, and
    symlinks that would escape the bundle root."""
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts or "\x00" in name:
        raise ValueError(f"unsafe bundle member: {name!r}")
    target = (target_home / name).resolve()
    home_resolved = target_home.resolve()
    if home_resolved != target and home_resolved not in target.parents:
        raise ValueError(f"bundle member escapes target: {name!r}")
    if member.issym():
        # Validate the link target after the link is created — if it
        # resolves outside target_home, refuse.
        link_target = member.linkname
        if Path(link_target).is_absolute():
            raise ValueError(
                f"bundle contains absolute symlink target: {name!r} → {link_target!r}"
            )
        # Defer the resolution check to after extraction (the link's
        # validity depends on its position).
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        import os as _os
        _os.symlink(link_target, str(target))
        # Sanity check: resolve the link and confirm it stays inside
        # target_home (or points at a not-yet-extracted sibling, which
        # is fine — it'll resolve once that sibling lands).
        try:
            real = (target.parent / link_target).resolve()
            if home_resolved != real and home_resolved not in real.parents:
                target.unlink(missing_ok=True)
                raise ValueError(
                    f"bundle symlink escapes target: {name!r} → {link_target!r}"
                )
        except (OSError, RuntimeError):
            pass
    elif member.isdir():
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif member.isfile():
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = tar.extractfile(member)
        if fh is None:
            return
        with target.open("wb") as out:
            shutil.copyfileobj(fh, out)
        target.chmod(member.mode & 0o700 | 0o400)


def _merge_db_rows_from_bundle(bundle_meta: dict, target_home: Path) -> None:
    """Write packages + vault_files rows into the target's vault.db."""
    db_path = target_home / "vault.db"
    # Reuse bubble.vault.db.init_db to ensure schema; it reads paths
    # from config so we briefly rebind.
    saved_home = config.BUBBLE_HOME
    config.BUBBLE_HOME = target_home
    config.VAULT_DIR = target_home / "vault"
    config.VAULT_DB = db_path
    config.STAGING_DIR = config.VAULT_DIR / ".staging"
    try:
        db.init_db()
        conn = db.connect()
        try:
            for b in bundle_meta["bundled_packages"]:
                vault_p = (target_home / "vault" / b["name"] /
                           b["version"] / b["wheel_tag"])
                conn.execute(
                    "INSERT OR REPLACE INTO packages "
                    "(name, version, wheel_tag, python_tag, abi_tag, platform_tag, "
                    " sha256, source, cached_at, last_used_at, vault_path, "
                    " has_native, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (b["name"], b["version"], b["wheel_tag"],
                     b.get("python_tag") or None,
                     b.get("abi_tag") or None,
                     b.get("platform_tag") or None,
                     b.get("sha256") or None,
                     b.get("source") or "bundle",
                     datetime.now().isoformat(),
                     datetime.now().isoformat(),
                     str(vault_p),
                     1 if b.get("has_native") else 0,
                     b.get("metadata_json") or "{}"),
                )
                conn.execute(
                    "DELETE FROM vault_files "
                    "WHERE package=? AND version=? AND wheel_tag=?",
                    (b["name"], b["version"], b["wheel_tag"]),
                )
            for f in bundle_meta["bundled_files"]:
                conn.execute(
                    "INSERT INTO vault_files "
                    "(package, version, wheel_tag, rel_path, sha256, size_bytes, mtime_ns) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (f["package"], f["version"], f["wheel_tag"],
                     f["rel_path"], f["sha256"], f["size_bytes"]),
                )

            # Write the shell row itself, with the source's metadata
            # blob intact — alias declarations and any other shell-level
            # facts ride through to the target's `shells` table.
            shell_name = bundle_meta["name"]
            shell_path = target_home / "shells" / shell_name
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO shells "
                "(name, created_at, last_used_at, shell_path, "
                " python_tag, lockfile, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (shell_name, now, now, str(shell_path),
                 bundle_meta["source_host"].get("python_tag"),
                 None,
                 bundle_meta.get("shell_metadata_json", "{}")),
            )
            conn.commit()
        finally:
            conn.close()
    finally:
        config.BUBBLE_HOME = saved_home
        config.VAULT_DIR = saved_home / "vault"
        config.VAULT_DB = saved_home / "vault.db"
        config.STAGING_DIR = config.VAULT_DIR / ".staging"


def _refresh_mtimes_after_extract(bundle_meta: dict, target_home: Path) -> None:
    """The bundle records 0 for mtime_ns; after extraction, write the
    actual on-disk mtimes so the stat fast-path in store.verify() works."""
    db_path = target_home / "vault.db"
    conn = sqlite3.connect(str(db_path))
    try:
        for f in bundle_meta["bundled_files"]:
            file_path = (target_home / "vault" / f["package"] /
                         f["version"] / f["wheel_tag"] / f["rel_path"])
            if not file_path.exists():
                continue
            try:
                mtime = file_path.stat().st_mtime_ns
            except OSError:
                continue
            conn.execute(
                "UPDATE vault_files SET mtime_ns=? "
                "WHERE package=? AND version=? AND wheel_tag=? AND rel_path=?",
                (mtime, f["package"], f["version"],
                 f["wheel_tag"], f["rel_path"]),
            )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────── bundle manifest parsing ────────────────


def _parse_bundle_manifest(text: str) -> dict:
    """Parse the small TOML subset the bundle manifest emits."""
    out: dict = {
        "name": "",
        "created_at": "",
        "bubble_version": "",
        "shell_metadata_json": "{}",
        "source_host": {},
        "packages": {},
        "aliases": {},
        "bundled_packages": [],
        "bundled_files": [],
    }
    section: Optional[str] = None
    array_section: Optional[str] = None
    current_array: Optional[dict] = None

    import re
    kv_re = re.compile(r'^([A-Za-z_]\w*)\s*=\s*(.+)$')

    def parse_value(s):
        s = s.strip()
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        try:
            return int(s)
        except ValueError:
            return s

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[[") and line.endswith("]]"):
            array_section = line[2:-2].strip()
            section = None
            current_array = {}
            out.setdefault(array_section, []).append(current_array)
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            array_section = None
            current_array = None
            continue
        m = kv_re.match(line)
        if not m:
            continue
        key, raw_val = m.group(1), m.group(2).strip()
        if section is None and array_section is None:
            out[key] = parse_value(raw_val)
        elif section == "source_host":
            out["source_host"][key] = parse_value(raw_val)
        elif section in ("packages", "aliases"):
            # inline-table rows like '"foo" = { version = "...", ... }' —
            # we don't strictly need to parse these on the unbundle side
            # because bundled_packages/bundled_files carry the load. Skip.
            continue
        elif current_array is not None:
            current_array[key] = parse_value(raw_val)
    return out
