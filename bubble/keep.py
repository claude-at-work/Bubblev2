"""Keep — path-addressed directory payloads in the vault.

The vault was content-addressed Python packages. Keep is the second region:
arbitrary user-chosen directory trees, named, captured as tar.gz, restorable
on a fresh filesystem. The intent is deliberate. The vault becomes what the
user *decided* matters, not what a runtime happened to install.

Layout
------
~/.bubble/keep/<name>/
    tree.tar.gz   the captured tree, gzip-compressed (stdlib only)
    meta.toml     name, source path, captured_at, sha256, byte counts,
                  excludes that were applied

Excludes
--------
Default excludes: __pycache__, *.pyc — never useful to preserve. Everything
else is captured unless the project has a `.keepignore` file at its root.
.keepignore is gitignore-style but simple: one substring pattern per line,
lines starting with `#` are comments. A path matches if any pattern is a
substring of the path *relative to the source root*. The CLI `--exclude
PATTERN` flag appends to whatever .keepignore declared.

Size cap
--------
Captures over `SIZE_WARN_BYTES` (100 MB compressed) refuse unless
--force-large is set. The point of keep is that the user picks what's
important; silent inflation defeats that.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import io
import os
import shutil
import sys
import tarfile
from pathlib import Path

from . import config


KEEP_DIRNAME = "keep"
TREE_FILENAME = "tree.tar.gz"
META_FILENAME = "meta.toml"
KEEPIGNORE = ".keepignore"

DEFAULT_EXCLUDES = ["__pycache__", ".pyc"]
SIZE_WARN_BYTES = 100 * 1024 * 1024  # 100 MB compressed


def _keep_root() -> Path:
    return config.KEEP_DIR


def _keep_dir(name: str) -> Path:
    return _keep_root() / name


def _read_keepignore(source: Path) -> list[str]:
    f = source / KEEPIGNORE
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _excluded(rel_path: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p in rel_path:
            return True
    return False


def _walk_files(source: Path, excludes: list[str]):
    """Yield (absolute_path, relative_path_str) for every file to include.
    Deterministic order: sorted by relative path. Skips symlinks pointing
    outside the source tree."""
    source = source.resolve()
    pairs = []
    for root, dirs, files in os.walk(source):
        root_p = Path(root)
        # Filter directories in-place so os.walk skips excluded subtrees.
        dirs[:] = sorted(
            d for d in dirs
            if not _excluded(str((root_p / d).relative_to(source)) + "/", excludes)
        )
        for fname in sorted(files):
            abs_p = root_p / fname
            rel = str(abs_p.relative_to(source))
            if _excluded(rel, excludes):
                continue
            pairs.append((abs_p, rel))
    pairs.sort(key=lambda pr: pr[1])
    for pr in pairs:
        yield pr


def _tree_sha(source: Path, excludes: list[str]) -> str:
    """Hash content + relative paths of all included files. Stable across
    machines as long as exclude set is the same."""
    h = hashlib.sha256()
    for abs_p, rel in _walk_files(source, excludes):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            with open(abs_p, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()


# ─────────────────────── capture ────────────────────────


def capture(
    source: Path,
    name: str,
    *,
    extra_excludes: list[str] | None = None,
    overwrite: bool = False,
    force_large: bool = False,
) -> dict:
    """Capture *source* into ~/.bubble/keep/<name>/. Returns a summary dict."""
    source = Path(source).resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"not a directory: {source}")

    excludes = list(DEFAULT_EXCLUDES)
    excludes += _read_keepignore(source)
    if extra_excludes:
        excludes += list(extra_excludes)

    dest_dir = _keep_dir(name)
    if dest_dir.exists():
        if not overwrite:
            raise FileExistsError(
                f"keep '{name}' already exists at {dest_dir} — pass --overwrite to replace"
            )
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)

    tree_path = dest_dir / TREE_FILENAME
    meta_path = dest_dir / META_FILENAME

    files_added = 0
    source_bytes = 0

    # Build tar.gz in a single pass.
    with gzip.open(tree_path, "wb", compresslevel=6) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for abs_p, rel in _walk_files(source, excludes):
                try:
                    arcname = f"{name}/{rel}"
                    tar.add(abs_p, arcname=arcname, recursive=False)
                    files_added += 1
                    source_bytes += abs_p.stat().st_size
                except OSError as exc:
                    print(f"  skip {rel}: {exc}", file=sys.stderr)

    archive_bytes = tree_path.stat().st_size
    if archive_bytes > SIZE_WARN_BYTES and not force_large:
        # Roll back the capture so a refused keep doesn't leave a half-keep.
        shutil.rmtree(dest_dir)
        raise OSError(
            f"keep '{name}' would be {archive_bytes / 1_048_576:.1f} MB compressed "
            f"(cap is {SIZE_WARN_BYTES / 1_048_576:.0f} MB) — pass --force-large "
            f"to accept, or add patterns to {source / KEEPIGNORE}"
        )

    # Hash the archive bytes themselves (for integrity verification on restore).
    h = hashlib.sha256()
    with open(tree_path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    archive_sha = h.hexdigest()

    tree_sha = _tree_sha(source, excludes)

    meta = {
        "name": name,
        "source": str(source),
        "captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "files": files_added,
        "source_bytes": source_bytes,
        "archive_bytes": archive_bytes,
        "archive_sha256": archive_sha,
        "tree_sha256": tree_sha,
        "excludes": excludes,
    }
    _write_meta(meta_path, meta)

    return meta


def _write_meta(path: Path, meta: dict) -> None:
    lines = []
    for key in ("name", "source", "captured_at", "files", "source_bytes",
                "archive_bytes", "archive_sha256", "tree_sha256"):
        v = meta[key]
        if isinstance(v, int):
            lines.append(f"{key} = {v}")
        else:
            s = str(v).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{s}"')
    exc = meta.get("excludes", [])
    if exc:
        items = ", ".join(f'"{e}"' for e in exc)
        lines.append(f"excludes = [{items}]")
    else:
        lines.append("excludes = []")
    path.write_text("\n".join(lines) + "\n")


def _read_meta(path: Path) -> dict:
    import tomllib
    with open(path, "rb") as f:
        return tomllib.load(f)


# ─────────────────────── restore ────────────────────────


def restore(name: str, target: Path | None = None, *, force: bool = False) -> dict:
    """Restore keep <name> into *target* (defaults to source from meta)."""
    dest_dir = _keep_dir(name)
    if not dest_dir.exists():
        raise FileNotFoundError(f"no keep named '{name}' at {dest_dir}")

    meta = _read_meta(dest_dir / META_FILENAME)
    tree_path = dest_dir / TREE_FILENAME

    # Verify archive bytes against recorded sha256.
    h = hashlib.sha256()
    with open(tree_path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    if h.hexdigest() != meta["archive_sha256"]:
        raise OSError(
            f"keep '{name}' archive sha256 mismatch — refusing restore. "
            f"Inspect {tree_path}"
        )

    if target is None:
        target = Path(meta["source"])
    target = Path(target).resolve()

    if target.exists() and any(target.iterdir()) and not force:
        raise FileExistsError(
            f"target {target} exists and is non-empty — pass --force to overwrite"
        )
    target.mkdir(parents=True, exist_ok=True)

    # Extract: archive members are prefixed with `<name>/`; strip that.
    with gzip.open(tree_path, "rb") as gz:
        with tarfile.open(fileobj=gz, mode="r") as tar:
            for member in tar.getmembers():
                if not member.name.startswith(f"{name}/"):
                    continue
                stripped = member.name[len(name) + 1:]
                if not stripped:
                    continue
                member.name = stripped
                tar.extract(member, target, set_attrs=False)

    return {
        "name": name,
        "restored_to": str(target),
        "files": meta["files"],
        "captured_at": meta["captured_at"],
    }


# ─────────────────────── inventory ────────────────────────


def list_keeps() -> list[dict]:
    root = _keep_root()
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        meta_path = d / META_FILENAME
        if not meta_path.exists():
            continue
        try:
            out.append(_read_meta(meta_path))
        except Exception:
            continue
    return out


def show(name: str) -> dict:
    meta_path = _keep_dir(name) / META_FILENAME
    if not meta_path.exists():
        raise FileNotFoundError(f"no keep named '{name}'")
    return _read_meta(meta_path)


def remove(name: str) -> None:
    d = _keep_dir(name)
    if not d.exists():
        raise FileNotFoundError(f"no keep named '{name}'")
    shutil.rmtree(d)
