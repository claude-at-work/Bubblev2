"""Keep — directory and file payloads in the vault, vault as canonical home.

The vault is where deliberately-chosen artifacts live. Keep is the region
for arbitrary user trees and standalone files — projects, third-party
repos, shell rc files — that survive a filesystem nuke and stay the
same bytes wherever they're referenced.

Inversion
---------
The earlier draft of keep stored each tree as a tar.gz under
`~/.bubble/keep/<name>/`. That made the vault a snapshot warehouse,
not a home. The current model flips it: the vault holds the live tree,
and the original filesystem location becomes a symlink pointing in.
Edits at the symlink edit the vault bytes directly. Nothing to refresh,
no drift to track.

Layout
------
~/.bubble/keep/<name>/             live tree (or live files for multi-file keeps)
~/.bubble/keep/.meta/<name>.toml   provenance: name, kind, captured_at,
                                   plus a list of (source_path, vault_relative)
                                   symlink pairs

Symlinks
--------
Capture moves the source into the vault and replaces the original path
with a symlink. For multi-file keeps (e.g. shell rc files), each file
is symlinked back to its own original location independently.

Wire / unwire are the nuke-recovery and reversal verbs: wire recreates
the source-path symlinks from meta; unwire removes them but leaves the
vault tree.

Activate
--------
When a keep has a `bin/` directory, `activate` symlinks each executable
into ~/.local/bin/ so the binaries become PATH-visible. Deactivate
removes those symlinks; the live tree stays in the vault.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
from pathlib import Path

from . import config


META_DIRNAME = ".meta"
BIN_SUBDIR = "bin"

_DEFAULT_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


# ─────────────────────── paths ────────────────────────


def _keep_root() -> Path:
    return config.KEEP_DIR


def _meta_root() -> Path:
    return _keep_root() / META_DIRNAME


def _keep_dir(name: str) -> Path:
    return _keep_root() / name


def _meta_path(name: str) -> Path:
    return _meta_root() / f"{name}.toml"


def _user_local_bin() -> Path:
    return Path.home() / ".local" / "bin"


def _validate_name(name: str) -> None:
    if not name or "/" in name or name in (".", "..") or name.startswith("."):
        raise ValueError(f"invalid keep name: {name!r}")


# ─────────────────────── capture (directory) ────────────────────────


def capture(
    source: Path,
    name: str | None = None,
    *,
    symlink_back: bool = True,
    overwrite: bool = False,
) -> dict:
    """Absorb *source* directory into the vault. Replaces the original
    path with a symlink pointing into the vault unless symlink_back=False.
    """
    source = Path(source).resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"not a directory: {source}")
    if name is None:
        name = source.name
    _validate_name(name)

    keep_root = _keep_root().resolve()
    try:
        source.relative_to(keep_root)
        raise ValueError(f"refuse: {source} is already inside the vault keep area")
    except ValueError as exc:
        if "already inside" in str(exc):
            raise

    dest = _keep_dir(name)
    if dest.exists() or dest.is_symlink():
        if not overwrite:
            raise FileExistsError(
                f"keep '{name}' exists at {dest} — pass --overwrite to replace"
            )
        if dest.is_symlink():
            dest.unlink()
        else:
            shutil.rmtree(dest)

    _meta_root().mkdir(parents=True, exist_ok=True)

    # Copy first so a mid-capture failure never leaves a hole at source.
    shutil.copytree(source, dest, symlinks=True, ignore=_DEFAULT_COPY_IGNORE)

    if symlink_back:
        _swap_to_symlink(source, dest)

    meta = {
        "name": name,
        "kind": "dir",
        "captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "symlinks": [{"from": str(source), "to": "."}] if symlink_back else [],
    }
    _write_meta(_meta_path(name), meta)
    return meta


def capture_files(
    files: list[Path],
    name: str,
    *,
    symlink_back: bool = True,
    overwrite: bool = False,
) -> dict:
    """Absorb a set of individual files into a single named keep. Each
    file is moved into the vault keep dir under its basename, then (if
    symlink_back) replaced at its original location with a symlink.
    """
    _validate_name(name)
    files = [Path(f).resolve() for f in files]
    for f in files:
        if not f.is_file() and not f.is_symlink():
            raise FileNotFoundError(f"not a file: {f}")
    if len({f.name for f in files}) != len(files):
        raise ValueError("file basenames must be unique within a keep")

    dest = _keep_dir(name)
    if dest.exists() or dest.is_symlink():
        if not overwrite:
            raise FileExistsError(
                f"keep '{name}' exists at {dest} — pass --overwrite to replace"
            )
        if dest.is_symlink():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _meta_root().mkdir(parents=True, exist_ok=True)

    symlinks = []
    for src in files:
        target = dest / src.name
        shutil.copy2(src, target, follow_symlinks=True)
        symlinks.append({"from": str(src), "to": src.name})

    if symlink_back:
        for src in files:
            target = dest / src.name
            _swap_to_symlink(src, target)

    meta = {
        "name": name,
        "kind": "files",
        "captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "symlinks": symlinks if symlink_back else [],
    }
    _write_meta(_meta_path(name), meta)
    return meta


def _swap_to_symlink(source: Path, target: Path) -> None:
    """Atomically replace `source` with a symlink to `target`. Renames
    source aside first so a failed symlink call can roll back."""
    swap = source.parent / (source.name + ".swap-keep")
    if swap.exists() or swap.is_symlink():
        if swap.is_dir() and not swap.is_symlink():
            shutil.rmtree(swap)
        else:
            swap.unlink()
    os.rename(str(source), str(swap))
    try:
        os.symlink(str(target), str(source))
    except OSError:
        os.rename(str(swap), str(source))
        raise
    if swap.is_dir() and not swap.is_symlink():
        shutil.rmtree(swap)
    else:
        swap.unlink()


# ─────────────────────── wire / unwire ────────────────────────


def wire(name: str) -> dict:
    """Recreate the source-path symlinks recorded in meta. For nuke recovery."""
    meta = _read_meta(name)
    dest = _keep_dir(name)
    if not dest.exists():
        raise FileNotFoundError(f"keep tree missing: {dest}")
    wired = []
    for entry in meta.get("symlinks", []):
        src = Path(entry["from"])
        target = dest if entry["to"] == "." else dest / entry["to"]
        if src.is_symlink():
            if Path(os.readlink(src)) == target:
                continue
            raise FileExistsError(
                f"refuse: {src} is a symlink to {os.readlink(src)} — "
                f"remove or rename first"
            )
        if src.exists():
            raise FileExistsError(f"refuse: {src} exists — remove or rename first")
        src.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target), str(src))
        wired.append({"from": str(src), "to": str(target)})
    return {"name": name, "wired": wired}


def unwire(name: str) -> dict:
    """Remove the source-path symlinks (leave vault contents intact)."""
    meta = _read_meta(name)
    removed = []
    for entry in meta.get("symlinks", []):
        src = Path(entry["from"])
        if src.is_symlink():
            src.unlink()
            removed.append(str(src))
    return {"name": name, "unwired": removed}


# ─────────────────────── activate (bin/ → ~/.local/bin) ────────────────────────


def _bin_dir(name: str) -> Path:
    return _keep_dir(name) / BIN_SUBDIR


def activate(name: str) -> dict:
    """Symlink each executable in keep/<name>/bin/ into ~/.local/bin/."""
    bd = _bin_dir(name)
    if not bd.is_dir():
        raise FileNotFoundError(f"keep '{name}' has no bin/ directory at {bd}")
    local_bin = _user_local_bin()
    local_bin.mkdir(parents=True, exist_ok=True)
    activated = []
    skipped = []
    for entry in sorted(bd.iterdir()):
        if entry.is_dir():
            continue
        target_resolved = entry.resolve()
        link = local_bin / entry.name
        if link.is_symlink():
            if Path(os.readlink(link)).resolve() == target_resolved:
                activated.append(str(link))
                continue
            skipped.append({"path": str(link),
                            "reason": f"existing symlink → {os.readlink(link)}"})
            continue
        if link.exists():
            skipped.append({"path": str(link), "reason": "non-symlink file exists"})
            continue
        os.symlink(str(target_resolved), str(link))
        activated.append(str(link))
    return {"name": name, "activated": activated, "skipped": skipped}


def deactivate(name: str) -> dict:
    """Remove ~/.local/bin/* symlinks that point into this keep's bin/."""
    bd = _bin_dir(name)
    if not bd.is_dir():
        return {"name": name, "deactivated": []}
    local_bin = _user_local_bin()
    removed = []
    for entry in bd.iterdir():
        link = local_bin / entry.name
        if not link.is_symlink():
            continue
        try:
            if Path(os.readlink(link)).resolve() == entry.resolve():
                link.unlink()
                removed.append(str(link))
        except OSError:
            continue
    return {"name": name, "deactivated": removed}


# ─────────────────────── inventory ────────────────────────


def list_keeps() -> list[dict]:
    root = _meta_root()
    if not root.exists():
        return []
    out = []
    for f in sorted(root.iterdir()):
        if f.suffix != ".toml":
            continue
        try:
            m = _load_meta_file(f)
            m["_size_bytes"] = _tree_size(_keep_dir(m["name"]))
            out.append(m)
        except Exception:
            continue
    return out


def show(name: str) -> dict:
    m = _read_meta(name)
    m["_size_bytes"] = _tree_size(_keep_dir(name))
    return m


def remove(name: str, *, unwire_first: bool = True) -> None:
    dest = _keep_dir(name)
    mpath = _meta_path(name)
    if not dest.exists() and not mpath.exists():
        raise FileNotFoundError(f"no keep named '{name}'")
    if unwire_first and mpath.exists():
        try:
            unwire(name)
        except (FileNotFoundError, FileExistsError):
            pass
        try:
            deactivate(name)
        except (FileNotFoundError, OSError):
            pass
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    if mpath.exists():
        mpath.unlink()


# ─────────────────────── resolve for `bubble run` ────────────────────────


def resolve_executable(name: str) -> Path | None:
    """Find an executable for `bubble run <name>`. Looks at bin/<name>
    first, then a single executable in bin/, then a top-level file with
    that name."""
    keep = _keep_dir(name)
    if not keep.exists():
        return None
    bd = keep / BIN_SUBDIR
    if bd.is_dir():
        named = bd / name
        if named.is_file() and os.access(named, os.X_OK):
            return named.resolve()
        execs = [p for p in bd.iterdir()
                 if p.is_file() and os.access(p, os.X_OK)]
        if len(execs) == 1:
            return execs[0].resolve()
    top = keep / name
    if top.is_file() and os.access(top, os.X_OK):
        return top.resolve()
    return None


# ─────────────────────── meta i/o ────────────────────────


def _tree_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _write_meta(path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'name = "{_escape(meta["name"])}"',
        f'kind = "{_escape(meta["kind"])}"',
        f'captured_at = "{_escape(meta["captured_at"])}"',
        "",
    ]
    for s in meta.get("symlinks", []):
        lines.append("[[symlinks]]")
        lines.append(f'from = "{_escape(s["from"])}"')
        lines.append(f'to = "{_escape(s["to"])}"')
        lines.append("")
    path.write_text("\n".join(lines))


def _escape(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _read_meta(name: str) -> dict:
    p = _meta_path(name)
    if not p.exists():
        raise FileNotFoundError(f"no keep named '{name}'")
    return _load_meta_file(p)


def _load_meta_file(p: Path) -> dict:
    import tomllib
    with open(p, "rb") as f:
        return tomllib.load(f)
