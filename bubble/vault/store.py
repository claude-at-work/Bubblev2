"""Vault store operations: add/remove/lookup with atomic writes.

`add_from_directory` is the unified entry point. Caller stages an unpacked
package directory anywhere; we move it to .staging, then rename into place.
A failed add leaves no half-populated vault entry.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .. import config
from . import db


_STDLIB: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())


# Strict allowlist for vault path segments. PEP 503 names are alnum/./-/_;
# wheel tags use the same plus '+'. Reject anything else, including null bytes.
_VAULT_SEG_RE = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")


def _safe_segment(s: str, kind: str) -> str:
    # The character class admits "." and ".." which would resolve under the
    # parent. Defense in depth: reject them at source even though is_under_vault
    # would also catch the actual escape downstream.
    if s in {".", ".."} or not _VAULT_SEG_RE.match(s):
        raise ValueError(f"unsafe vault {kind!r}: {s!r}")
    return s


# Module-level audit log. Every time vault-add inserts a top_level row whose
# import_name is already claimed by a different (package, version, wheel_tag),
# we append a structured entry here. First-claim semantics are unchanged
# (meta_finder picks by version/tag); the log just makes the contention
# observable instead of a silent accident.
top_level_contentions: list[dict] = []


def vault_path_for(name: str, version: str, wheel_tag: str) -> Path:
    return (config.VAULT_DIR
            / _safe_segment(name, "name")
            / _safe_segment(version, "version")
            / _safe_segment(wheel_tag, "wheel_tag"))


def is_under_vault(path: Path) -> bool:
    """Confirm a path resolves inside VAULT_DIR. Use before linking from a DB-supplied path."""
    try:
        resolved = Path(path).resolve()
        vault_root = config.VAULT_DIR.resolve()
    except OSError:
        return False
    return resolved == vault_root or vault_root in resolved.parents


def has(conn: sqlite3.Connection, name: str, version: str, wheel_tag: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM packages WHERE name=? AND version=? AND wheel_tag=?",
        (name, version, wheel_tag),
    ).fetchone()
    return row is not None


def find_versions(conn: sqlite3.Connection, name: str) -> list[tuple[str, str, str]]:
    """Return [(version, wheel_tag, vault_path), ...] for a package name."""
    return list(conn.execute(
        "SELECT version, wheel_tag, vault_path FROM packages WHERE name=? "
        "ORDER BY version DESC, wheel_tag",
        (name,),
    ).fetchall())


def stage_dir() -> Path:
    """Allocate a fresh staging directory. 0o700 — package payloads can include
    files users wouldn't expect to be world-readable (tokens in dist-info, etc)."""
    config.STAGING_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = config.STAGING_DIR / uuid.uuid4().hex
    p.mkdir(mode=0o700)
    return p


def _detect_native(root: Path) -> bool:
    for ext in (".so", ".dylib", ".pyd"):
        if next(root.rglob(f"*{ext}"), None):
            return True
    return False


def _walk_for_integrity(root: Path) -> tuple[bool, list[tuple[str, str, int, int]]]:
    """One traversal, two outputs: native-detection and per-file integrity rows.

    Returns (has_native, rows) where each row is
    (rel_path, sha256, size_bytes, mtime_ns). Symlinks are skipped — vault
    contents are byte-stable, and a symlinked file would record the link
    target's bytes under the link's name, dissolving the integrity edge.
    """
    has_native = False
    rows: list[tuple[str, str, int, int]] = []
    native_exts = {".so", ".dylib", ".pyd"}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        if path.suffix in native_exts:
            has_native = True
        rows.append((rel, _hash_subtree(path), st.st_size, st.st_mtime_ns))
    return has_native, rows


def _hash_file(p: Path) -> bytes:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        while True:
            chunk = fh.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.digest()


def _hash_subtree(root: Path) -> str:
    """Deterministic content hash over a file or directory subtree.

    Same bytes under the same relative paths → same digest. Any file
    addition, removal, rename, or content change changes the digest. This
    is the cryptographic edge between an import name and the bytes the
    vault serves under it.
    """
    h = hashlib.sha256()
    if root.is_file():
        h.update(b"FILE\x00\x00")
        h.update(_hash_file(root))
        return h.hexdigest()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix().encode("utf-8")
        if p.is_dir():
            h.update(b"DIR\x00")
            h.update(rel)
            h.update(b"\x00")
        elif p.is_file():
            h.update(b"FILE\x00")
            h.update(rel)
            h.update(b"\x00")
            h.update(_hash_file(p))
    return h.hexdigest()


def _top_level_subpath(vault_path: Path, name: str) -> Optional[Path]:
    """Resolve an import name to a real subpath in the vault tree.

    Used in verify-mode: a top_level.txt assertion is recorded only if the
    name corresponds to a real importable target.
    """
    pkg = vault_path / name
    if pkg.is_dir() and (pkg / "__init__.py").exists():
        return pkg
    py = vault_path / f"{name}.py"
    if py.is_file():
        return py
    for entry in vault_path.iterdir():
        if entry.suffix in (".so", ".pyd") and entry.stem.split(".")[0] == name:
            return entry
    return None


def commit(
    *,
    name: str,
    version: str,
    wheel_tag: str,
    python_tag: str,
    abi_tag: str,
    platform_tag: str,
    staged: Path,
    source: str,
    sha256: Optional[str] = None,
    metadata: Optional[dict] = None,
    overwrite: bool = False,
) -> Path:
    """Move a fully-prepared staging dir into the vault and index it.

    Atomicity: the move is a single os.rename within the same filesystem.
    On failure, the staging dir is left for inspection.
    """
    final = vault_path_for(name, version, wheel_tag)
    if final.exists():
        if not overwrite:
            shutil.rmtree(staged, ignore_errors=True)
            return final
        shutil.rmtree(final)
    final.parent.mkdir(parents=True, exist_ok=True)

    # Cross-fs safety: rename if same fs, else copy+remove
    try:
        os.rename(staged, final)
    except OSError:
        shutil.copytree(staged, final, symlinks=True)
        shutil.rmtree(staged, ignore_errors=True)

    has_native, integrity_rows = _walk_for_integrity(final)
    now = datetime.now().isoformat()
    conn = db.connect()
    conn.execute(
        "INSERT OR REPLACE INTO packages "
        "(name, version, wheel_tag, python_tag, abi_tag, platform_tag, "
        " sha256, source, cached_at, last_used_at, vault_path, has_native, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, version, wheel_tag, python_tag, abi_tag, platform_tag,
         sha256, source, now, now, str(final), int(has_native),
         json.dumps(metadata or {})),
    )

    # Replace any prior vault_files rows for this key — re-add overwrites
    # cleanly, like the other per-package fact tables.
    conn.execute(
        "DELETE FROM vault_files WHERE package=? AND version=? AND wheel_tag=?",
        (name, version, wheel_tag),
    )
    if integrity_rows:
        conn.executemany(
            "INSERT INTO vault_files (package, version, wheel_tag, "
            "rel_path, sha256, size_bytes, mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(name, version, wheel_tag, rel, sha, sz, mt)
             for rel, sha, sz, mt in integrity_rows],
        )

    # Index top-level importable names — the bridge from Python's import space
    # to the vault's PyPI-name space. Try top_level.txt first (verified against
    # the staged tree), fall back to scanning for dirs/.py files at the root.
    # Each row carries a content hash of the subtree it claims, computed once
    # here, so the import→artifact edge is cryptographic, not nominal.
    discovered = _discover_top_levels(final)
    own_imports = {n for n, _ in discovered}

    others_by_name: dict[str, list[tuple[str, str, str]]] = {}
    if discovered:
        placeholders = ",".join("?" * len(discovered))
        for imp, pkg, ver, tag in conn.execute(
            f"SELECT import_name, package, version, wheel_tag FROM top_level "
            f"WHERE import_name IN ({placeholders}) "
            f"AND NOT (package=? AND version=? AND wheel_tag=?)",
            (*[d[0] for d in discovered], name, version, wheel_tag),
        ):
            others_by_name.setdefault(imp, []).append((pkg, ver, tag))

    conn.execute(
        "DELETE FROM top_level WHERE package=? AND version=? AND wheel_tag=?",
        (name, version, wheel_tag),
    )
    for top_name, top_sha in discovered:
        contenders = others_by_name.get(top_name, [])
        if contenders:
            top_level_contentions.append({
                "import_name": top_name,
                "incoming": (name, version, wheel_tag),
                "incoming_sha256": top_sha,
                "existing": contenders,
            })
        conn.execute(
            "INSERT INTO top_level "
            "(package, version, wheel_tag, import_name, import_sha256) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, version, wheel_tag, top_name, top_sha),
        )

    _index_package_internals(conn, name, version, wheel_tag, final, own_imports)

    conn.commit()
    conn.close()
    return final


def _discover_top_levels(vault_path: Path) -> list[tuple[str, str]]:
    """Find the top-level importable names this package contributes, paired
    with a content hash of each subtree.

    Verify mode: a name asserted by `top_level.txt` is recorded only if it
    resolves to a real subpath in the staged tree. An asserted name with no
    corresponding directory or file is silently dropped — we'd rather
    under-claim than record a binding the bytes can't honor.
    """
    asserted: set[str] = set()
    for tl_file in vault_path.rglob("top_level.txt"):
        for line in tl_file.read_text(errors="replace").splitlines():
            n = line.strip()
            if n and not n.startswith("#"):
                asserted.add(n.split("/")[-1])

    cache: dict[Path, str] = {}
    def digest(p: Path) -> str:
        if p not in cache:
            cache[p] = _hash_subtree(p)
        return cache[p]

    if asserted:
        verified: list[tuple[str, str]] = []
        for n in sorted(asserted):
            sub = _top_level_subpath(vault_path, n)
            if sub is not None:
                verified.append((n, digest(sub)))
        if verified:
            return verified

    # Fallback: scan the vault dir's top-level entries.
    found: list[tuple[str, str]] = []
    for entry in sorted(vault_path.iterdir()):
        if entry.name.endswith(".dist-info") or entry.name.endswith(".data"):
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            found.append((entry.name, digest(entry)))
        elif entry.suffix == ".py" and entry.stem != "__init__":
            found.append((entry.stem, digest(entry)))
        elif entry.suffix in (".so", ".pyd"):
            # foo.cpython-313-aarch64-linux-gnu.so → foo
            stem = entry.stem.split(".")[0]
            found.append((stem, digest(entry)))
    return found


# ─────────────────────── per-package indexing ───────────────────────


# Skip path components that aren't part of the importable surface.
_SKIP_DIR_SUFFIXES = (".dist-info", ".data")
_SKIP_DIR_NAMES = {"__pycache__"}


def _walk_modules(vault_path: Path) -> Iterator[tuple[Path, str, bool]]:
    """Yield (path, dotted_module_name, is_native) for each importable module.

    Walks the staged tree, skipping `*.dist-info`, `*.data`, and `__pycache__`.
    Native modules (`.so`/`.pyd`) have their ABI tags stripped from the stem.
    """
    for path in sorted(vault_path.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(vault_path)
        if any(part in _SKIP_DIR_NAMES or part.endswith(_SKIP_DIR_SUFFIXES)
               for part in rel.parts[:-1]):
            continue
        # Top-level dist-info / data files — these directories are filtered
        # above for nested files; leaf-level filter for the root level.
        if rel.parts and (rel.parts[0].endswith(_SKIP_DIR_SUFFIXES)
                          or rel.parts[0] in _SKIP_DIR_NAMES):
            continue

        parent = rel.parent
        parent_parts = parent.parts if parent != Path(".") else ()
        if path.suffix == ".py":
            if path.name == "__init__.py":
                if not parent_parts:
                    continue  # stray __init__.py at vault root
                mod = ".".join(parent_parts)
            else:
                mod = ".".join(parent_parts + (path.stem,))
            yield (path, mod, False)
        elif path.suffix in (".so", ".pyd"):
            stem = path.stem.split(".")[0]  # strip ABI tags like .cpython-313-aarch64
            mod = ".".join(parent_parts + (stem,)) if parent_parts else stem
            yield (path, mod, True)


def _imports_for_source(source: str, filename: str) -> set[str]:
    """AST-walk a Python source string and collect top-level import targets."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imports.add(node.module)
    return imports


# PEP 508-ish: name [extras] [version_spec] [; marker]. We don't validate the
# marker — just slice out the extra="..." case for the optional flag.
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_REQ_EXTRA_RE = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")


def _parse_requires_dist(line: str) -> Optional[tuple[str, str, bool, Optional[str]]]:
    """Parse a single Requires-Dist value → (name, version_spec, optional, extra).

    Examples it handles:
      'urllib3<3,>=1.21.1'                       → ('urllib3', '<3,>=1.21.1', False, None)
      'idna (<4,>=2.5); python_version >= "3.5"' → ('idna', '<4,>=2.5', False, None)
      'pytest; extra == "test"'                  → ('pytest', '', True, 'test')
    """
    main, _, marker = line.partition(";")
    m = _REQ_NAME_RE.match(main.strip())
    if not m:
        return None
    name = m.group(1)
    spec = m.group(3).strip()
    if spec.startswith("(") and spec.endswith(")"):
        spec = spec[1:-1].strip()
    extra: Optional[str] = None
    if marker:
        em = _REQ_EXTRA_RE.search(marker)
        if em:
            extra = em.group(1)
    return (name, spec, extra is not None, extra)


def _index_package_internals(
    conn: sqlite3.Connection,
    name: str,
    version: str,
    wheel_tag: str,
    vault_path: Path,
    own_imports: set[str],
) -> None:
    """Populate modules / module_imports / dependencies for the just-vaulted pkg.

    Idempotent: clears any prior rows under (name, version, wheel_tag) before
    writing — re-add overwrites cleanly.
    """
    for tbl in ("module_imports", "modules", "dependencies"):
        conn.execute(
            f"DELETE FROM {tbl} WHERE package=? AND version=? AND wheel_tag=?",
            (name, version, wheel_tag),
        )

    for path, mod_name, is_native in _walk_modules(vault_path):
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        conn.execute(
            "INSERT INTO modules (package, version, wheel_tag, module_name, "
            "module_path, is_native, size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, version, wheel_tag, mod_name, str(path), int(is_native), size),
        )
        if is_native:
            continue
        try:
            source = path.read_text(errors="replace")
        except OSError:
            continue
        raw = _imports_for_source(source, str(path))
        if not raw:
            continue
        external = sorted({
            full.split(".")[0] for full in raw
            if full.split(".")[0] not in _STDLIB
            and full.split(".")[0] not in own_imports
        })
        conn.execute(
            "INSERT INTO module_imports (package, version, wheel_tag, "
            "module_name, imports, imports_external) VALUES (?, ?, ?, ?, ?, ?)",
            (name, version, wheel_tag, mod_name,
             json.dumps(sorted(raw)), json.dumps(external)),
        )

    # dependencies — parse Requires-Dist from the dist-info METADATA.
    from . import metadata as meta
    for di in vault_path.glob("*.dist-info"):
        md_path = di / "METADATA"
        if not md_path.exists():
            continue
        try:
            headers = meta.parse_metadata(md_path.read_text(errors="replace"))
        except OSError:
            break
        for line in headers.get("requires_dist", []):
            parsed = _parse_requires_dist(line)
            if not parsed:
                continue
            dep_name, spec, optional, extra = parsed
            conn.execute(
                "INSERT INTO dependencies (package, version, wheel_tag, "
                "dep_name, dep_version_spec, optional, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, version, wheel_tag, dep_name, spec, int(optional), extra),
            )
        break  # one dist-info per package


# ─────────────────────── per-file integrity ──────────────────────────


@dataclass
class VerifyReport:
    """Pure data return from `verify()`. Callers decide what to do.

    `matched` files passed every check. `drifted` are files whose bytes
    no longer match what was vaulted. `missing` are files the index
    expected and the disk does not have. `extra` are files on disk that
    the index doesn't list — usually a sign of accidental writes into
    the vault tree.

    `had_index` is False when no `vault_files` rows exist for this
    (package, version, wheel_tag) — true for vaults from before schema
    v3, or for entries that haven't been rehashed. Callers that need a
    decision have to pick a policy: refuse without an index, or fall
    through. The function gives back the fact, not the policy.
    """
    package: str
    version: str
    wheel_tag: str
    had_index: bool = False
    matched: list[str] = field(default_factory=list)
    drifted: list[tuple[str, str]] = field(default_factory=list)   # (rel, kind)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def clean(self) -> bool:
        return not (self.drifted or self.missing or self.extra)


def verify(name: str, version: str, wheel_tag: str) -> VerifyReport:
    """Compare on-disk vault tree to recorded `vault_files` facts.

    Stat fast-path: if size and mtime_ns match what was recorded, the
    file is matched without rehashing. On any disagreement the file is
    rehashed; a hash mismatch is `vault_drift_modified`, a size mismatch
    that the rehash confirms is also `vault_drift_modified`. Files
    listed but absent are `vault_drift_missing`. Files present that
    aren't listed are reported as `extra` (caller decides whether
    `vault_drift_extra` is the right kind to record or whether the
    presence is benign — e.g. .pyc files written at import time).
    """
    import time
    t0 = time.monotonic()

    final = vault_path_for(name, version, wheel_tag)
    report = VerifyReport(package=name, version=version, wheel_tag=wheel_tag)

    conn = db.connect()
    try:
        rows = list(conn.execute(
            "SELECT rel_path, sha256, size_bytes, mtime_ns FROM vault_files "
            "WHERE package=? AND version=? AND wheel_tag=?",
            (name, version, wheel_tag),
        ))
    finally:
        conn.close()

    if not rows:
        report.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return report  # had_index = False
    report.had_index = True

    indexed: dict[str, tuple[str, int, int]] = {
        rel: (sha, size, mtime) for rel, sha, size, mtime in rows
    }

    seen_on_disk: set[str] = set()
    for path in final.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(final).as_posix()
        seen_on_disk.add(rel)
        ix = indexed.get(rel)
        if ix is None:
            report.extra.append(rel)
            continue
        sha, size, mtime = ix
        try:
            st = path.stat()
        except OSError:
            report.missing.append(rel)
            continue
        if st.st_size == size and st.st_mtime_ns == mtime:
            report.matched.append(rel)
            continue
        # Stat says drift; confirm with rehash.
        actual = _hash_subtree(path)
        if actual == sha:
            report.matched.append(rel)
        else:
            report.drifted.append((rel, "vault_drift_modified"))

    for rel in indexed:
        if rel not in seen_on_disk:
            report.missing.append(rel)

    report.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return report


def remove(name: str, version: str, wheel_tag: str) -> bool:
    """Delete a vault entry and its tree."""
    conn = db.connect()
    row = conn.execute(
        "SELECT vault_path FROM packages WHERE name=? AND version=? AND wheel_tag=?",
        (name, version, wheel_tag),
    ).fetchone()
    if not row:
        conn.close()
        return False
    path = Path(row[0])
    conn.execute(
        "DELETE FROM packages WHERE name=? AND version=? AND wheel_tag=?",
        (name, version, wheel_tag),
    )
    conn.commit()
    conn.close()
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    return True


def list_all(conn: Optional[sqlite3.Connection] = None) -> list[tuple]:
    """Return rows from packages table."""
    own = conn is None
    if own:
        conn = db.connect()
    rows = list(conn.execute(
        "SELECT name, version, wheel_tag, has_native, source, cached_at, vault_path "
        "FROM packages ORDER BY name, version"
    ))
    if own:
        conn.close()
    return rows


def touch(name: str, version: str, wheel_tag: str) -> None:
    """Update last_used_at — for GC."""
    conn = db.connect()
    conn.execute(
        "UPDATE packages SET last_used_at=? WHERE name=? AND version=? AND wheel_tag=?",
        (datetime.now().isoformat(), name, version, wheel_tag),
    )
    conn.commit()
    conn.close()
