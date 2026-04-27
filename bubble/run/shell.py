"""Long-lived named bubble — venv-shape view over the vault content store.

A shell is one version per package name (Python import semantics demand this),
linked from the vault. The manifest.toml is the externally-readable contract;
the shells DB row is internal bookkeeping.

Shell layout:
    ~/.bubble/shells/<name>/
        lib/<package>            -> vault symlink (whole package)
        bin/<entry-point>        -> generated console-script wrapper
        activate                 # POSIX sh, sourceable
        python                   # wrapper exec
        manifest.toml            # externally-readable contract
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import config
from ..vault import db, store, metadata as meta


# ───────────────────────────── spec parsing ─────────────────────────────


_SPEC_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(?:==(.+))?$")


def parse_spec(spec: str) -> tuple[str, Optional[str]]:
    """Parse 'requests' or 'requests==2.31.0'. PEP 440 ranges not yet supported."""
    m = _SPEC_RE.match(spec.strip())
    if not m:
        raise ValueError(f"unparseable spec: {spec!r}")
    return m.group(1), m.group(2)


# ───────────────────── version & wheel-tag resolution ────────────────────


def _wheel_tag_score(tag: str) -> int:
    """Higher is better-matching for the current runner.

    Scoring is intentionally simple — exact (py+abi+plat) match wins, then
    interpreter-major match, then pure-python.
    """
    runner_py = config.runner_python_tag()
    runner_plat = config.runner_platform_tag()
    parts = tag.split("-")
    if len(parts) != 3:
        return 0
    py, abi, plat = parts
    score = 0
    if py == runner_py:
        score += 100
    elif py == "py3" or py.startswith("py3"):
        score += 30
    elif py.startswith(runner_py[:2]):  # cp* matches cp*
        score += 20
    if abi in ("none", "abi3"):
        score += 10
    elif abi == runner_py:
        score += 50
    if plat == "any":
        score += 5
    elif runner_plat in plat or plat in runner_plat:
        score += 40
    return score


def best_version(conn: sqlite3.Connection, name: str,
                 pinned_version: Optional[str] = None) -> Optional[tuple[str, str, str]]:
    """Pick (version, wheel_tag, vault_path) for a package.

    If pinned_version: must match. Otherwise highest version.
    Within a version, pick the wheel-tag with highest score for the runner.
    PEP 503: matches pydantic-core/pydantic_core/Pydantic.Core interchangeably.
    """
    rows = store.find_versions(conn, name)
    if not rows:
        # Try PEP 503 normalized variants
        norm = meta.normalize_name(name)
        candidates = conn.execute(
            "SELECT name FROM packages GROUP BY name"
        ).fetchall()
        for (cand_name,) in candidates:
            if meta.normalize_name(cand_name) == norm:
                rows = store.find_versions(conn, cand_name)
                if rows:
                    break
    if not rows:
        return None
    if pinned_version:
        rows = [r for r in rows if r[0] == pinned_version]
        if not rows:
            return None

    # Group by version; pick highest version (string-sort approximates PEP 440)
    rows.sort(key=lambda r: _version_key(r[0]), reverse=True)
    target_version = rows[0][0]
    same = [r for r in rows if r[0] == target_version]
    same.sort(key=lambda r: _wheel_tag_score(r[1]), reverse=True)
    return same[0]


def _version_key(v: str) -> tuple:
    """Cheap PEP 440-ish sort key. Splits on dots, ints when possible."""
    parts = []
    for chunk in re.split(r"[.\-+]", v):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


# ───────────────────────── entry-point extraction ────────────────────────


def _entry_points_for(vault_path: Path) -> list[tuple[str, str, str]]:
    """Return list of (script_name, module, attr) from any entry_points.txt
    or RECORD-discoverable entry_points within the package's dist-info.

    The dist-info ends up under vault_path because RECORD-listed files
    include it. Find any *.dist-info/entry_points.txt.
    """
    out = []
    for ep_file in vault_path.rglob("entry_points.txt"):
        section = None
        for line in ep_file.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if section != "console_scripts":
                continue
            if "=" not in line:
                continue
            name, _, target = line.partition("=")
            name = name.strip()
            target = target.strip()
            if ":" in target:
                module, _, attr = target.partition(":")
            else:
                module, attr = target, "main"
            out.append((name, module.strip(), attr.strip()))
    return out


_CONSOLE_WRAPPER_TMPL = """#!{python}
# bubble shell entry-point wrapper for {script_name}
import sys
from {module} import {attr_root}
if __name__ == "__main__":
    sys.exit({attr}() if callable({attr}) else 0)
"""


def _write_console_wrapper(path: Path, python: str, module: str, attr: str,
                           script_name: str) -> None:
    attr_root = attr.split(".")[0]
    content = _CONSOLE_WRAPPER_TMPL.format(
        python=python,
        script_name=script_name,
        module=module,
        attr=attr,
        attr_root=attr_root,
    )
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ─────────────────────────── activate / launcher ─────────────────────────


_ACTIVATE_TMPL = """# bubble shell activate — source this from POSIX sh / bash / zsh
# usage: source {shell_dir}/activate
_BUBBLE_OLD_PYTHONPATH="${{PYTHONPATH:-}}"
_BUBBLE_OLD_PATH="$PATH"
export PYTHONPATH="{shell_lib}${{PYTHONPATH:+:$PYTHONPATH}}"
export PATH="{shell_bin}:$PATH"
export BUBBLE_SHELL="{name}"
export BUBBLE_SHELL_DIR="{shell_dir}"

bubble_deactivate() {{
    export PYTHONPATH="$_BUBBLE_OLD_PYTHONPATH"
    export PATH="$_BUBBLE_OLD_PATH"
    unset BUBBLE_SHELL BUBBLE_SHELL_DIR _BUBBLE_OLD_PYTHONPATH _BUBBLE_OLD_PATH
    unset -f bubble_deactivate
}}
"""


_PYTHON_LAUNCHER_TMPL = """#!/bin/sh
# bubble shell python launcher
exec {python} "$@"
"""


def _write_activate(shell_dir: Path, name: str) -> None:
    activate = shell_dir / "activate"
    activate.write_text(_ACTIVATE_TMPL.format(
        shell_dir=str(shell_dir),
        shell_lib=str(shell_dir / "lib"),
        shell_bin=str(shell_dir / "bin"),
        name=name,
    ))
    py_launcher = shell_dir / "python"
    py_launcher.write_text(_PYTHON_LAUNCHER_TMPL.format(python=sys.executable))
    py_launcher.chmod(py_launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ─────────────────────────── manifest.toml ───────────────────────────────


def _write_manifest(shell_dir: Path, name: str, packages: dict) -> None:
    """packages: { pkg_name: {'version': v, 'wheel_tag': t} }"""
    lines = [
        f'# bubble shell manifest — externally-readable contract',
        f'name = "{name}"',
        f'created_at = "{datetime.now().isoformat()}"',
        f'',
        f'[packages]',
    ]
    for pkg in sorted(packages):
        info = packages[pkg]
        lines.append(f'"{pkg}" = {{ version = "{info["version"]}", wheel_tag = "{info["wheel_tag"]}" }}')
    (shell_dir / "manifest.toml").write_text("\n".join(lines) + "\n")


def _read_manifest(shell_dir: Path) -> dict:
    """Cheap TOML reader for manifest.toml. Only handles the format we write."""
    path = shell_dir / "manifest.toml"
    if not path.exists():
        return {}
    pkgs = {}
    in_section = False
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("[packages]"):
            in_section = True
            continue
        if line.startswith("[") and line != "[packages]":
            in_section = False
            continue
        if not in_section or not line or line.startswith("#"):
            continue
        # "name" = { version = "v", wheel_tag = "t" }
        m = re.match(r'"([^"]+)"\s*=\s*\{\s*version\s*=\s*"([^"]+)"\s*,'
                     r'\s*wheel_tag\s*=\s*"([^"]+)"\s*\}', line)
        if m:
            pkgs[m.group(1)] = {"version": m.group(2), "wheel_tag": m.group(3)}
    return pkgs


# ─────────────────────────── shell operations ────────────────────────────


def shell_dir(name: str) -> Path:
    if not re.match(r"^[A-Za-z0-9_\-]{1,64}$", name):
        raise ValueError(f"shell names must be [A-Za-z0-9_-]+ up to 64 chars, got {name!r}")
    return config.SHELLS_DIR / name


def _link_package(shell_lib: Path, vault_path: Path, pkg_name: str,
                  *, version: Optional[str] = None,
                  wheel_tag: Optional[str] = None) -> list[str]:
    """Symlink importable modules from vault_path into shell_lib.

    'Importable modules' means top-level entries that don't end in .dist-info.
    Whole-package symlinks (data files come along for free).
    Returns list of importable names linked.

    Integrity: when (version, wheel_tag) are provided and BUBBLE_VERIFY!=0,
    the vault entry is verified before linking. Drift refuses the link and
    surfaces a `[[failures]]` entry through host.record_failure.

    Relocatability: symlinks are emitted relative to the shell-lib directory,
    so a wholesale move of BUBBLE_HOME (vault + shells together) preserves
    every link. The bundle thread depends on this property — without it, an
    untar on a target machine produces a tree of dangling links. With it,
    the tree just works at whatever path it was extracted to.
    """
    if not store.is_under_vault(vault_path):
        raise ValueError(f"refusing to link from outside the vault: {vault_path}")
    if version and wheel_tag and os.environ.get("BUBBLE_VERIFY") != "0":
        from .. import host
        report = store.verify(pkg_name, version, wheel_tag)
        if report.had_index and not report.clean:
            target = f"{pkg_name}=={version}@{wheel_tag}"
            for rel, kind in report.drifted:
                host.record_failure(kind, target, f"rel={rel}")
            for rel in report.missing:
                host.record_failure("vault_drift_missing", target, f"rel={rel}")
            raise RuntimeError(
                f"vault drift refusing to link {target}: "
                f"{len(report.drifted)} modified, {len(report.missing)} missing. "
                f"Run `bubble vault rehash {pkg_name} {version} {wheel_tag}` "
                f"to re-record, or `bubble vault remove ...` to drop the entry."
            )
    linked = []
    shell_lib.mkdir(parents=True, exist_ok=True)
    for entry in vault_path.iterdir():
        if entry.name.endswith(".dist-info"):
            continue
        if entry.name.endswith(".data"):
            # wheel .data dir has scripts/headers/data subtrees; skip the
            # top-level wrapper (entry-points handled separately)
            continue
        dest = shell_lib / entry.name
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        # Emit a relative symlink: target is computed from dest's parent
        # to entry, so a relocated tree resolves the link against its
        # new container instead of dangling at the old absolute path.
        rel_target = os.path.relpath(entry, start=dest.parent)
        os.symlink(rel_target, dest)
        linked.append(entry.name)
    return linked


def _link_entry_points(shell_bin: Path, vault_path: Path, python: str) -> list[str]:
    shell_bin.mkdir(parents=True, exist_ok=True)
    written = []
    for script_name, module, attr in _entry_points_for(vault_path):
        wrapper = shell_bin / script_name
        if wrapper.exists():
            wrapper.unlink()
        _write_console_wrapper(wrapper, python, module, attr, script_name)
        written.append(script_name)
    return written


def create(name: str, specs: list[str], *, exist_ok: bool = False) -> Path:
    """Create a new shell with optional initial packages."""
    sd = shell_dir(name)
    if sd.exists():
        if not exist_ok:
            raise FileExistsError(f"shell already exists: {name}")
    else:
        sd.mkdir(parents=True)
        (sd / "lib").mkdir()
        (sd / "bin").mkdir()
    _write_activate(sd, name)
    _write_manifest(sd, name, {})
    conn = db.connect()
    conn.execute(
        "INSERT OR REPLACE INTO shells (name, created_at, last_used_at, "
        "shell_path, python_tag, lockfile, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, datetime.now().isoformat(), datetime.now().isoformat(),
         str(sd), config.runner_python_tag(), None, json.dumps({})),
    )
    conn.commit()
    conn.close()
    if specs:
        add(name, specs)
    return sd


def add(name: str, specs: list[str]) -> dict:
    """Add packages to an existing shell.

    Each spec is 'pkg' or 'pkg==version'. Picks the best wheel-tag for the
    runner; raises if no compatible tag found.
    """
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")
    pkgs = _read_manifest(sd)
    conn = db.connect()
    summary = {"linked": [], "scripts": [], "missing": [], "conflicts": []}
    from .. import host
    try:
        for spec in specs:
            pkg, ver = parse_spec(spec)
            chosen = best_version(conn, pkg, ver)
            if not chosen:
                summary["missing"].append(spec)
                host.record_failure(
                    "shell_pkg_missing", spec,
                    f"shell={name}; spec did not resolve in vault",
                )
                continue
            version, wheel_tag, vault_path = chosen
            existing = pkgs.get(pkg)
            if existing and (existing["version"] != version
                             or existing["wheel_tag"] != wheel_tag):
                summary["conflicts"].append(
                    (pkg, existing, {"version": version, "wheel_tag": wheel_tag})
                )
                host.record_failure(
                    "shell_version_conflict", pkg,
                    f"shell={name}; existing={existing}; requested="
                    f"{{'version':'{version}','wheel_tag':'{wheel_tag}'}}",
                )
                continue
            linked = _link_package(sd / "lib", Path(vault_path), pkg,
                                   version=version, wheel_tag=wheel_tag)
            scripts = _link_entry_points(sd / "bin", Path(vault_path),
                                         python=str(sd / "python"))
            store.touch(pkg, version, wheel_tag)
            pkgs[pkg] = {"version": version, "wheel_tag": wheel_tag}
            summary["linked"].append((pkg, version, wheel_tag, len(linked)))
            summary["scripts"].extend(scripts)
        _write_manifest(sd, name, pkgs)
        conn.execute(
            "UPDATE shells SET last_used_at=? WHERE name=?",
            (datetime.now().isoformat(), name),
        )
        conn.commit()
    finally:
        conn.close()
    return summary


def add_pinned(name: str, pkg_name: str, version: str, wheel_tag: str) -> dict:
    """Add an exactly-pinned (name, version, wheel_tag) to a shell.

    Distinct from `add()`, which takes free-form specs and lets
    `best_version` resolve them. The deployment-manifest path needs the
    triplet to round-trip exactly as written, so this function bypasses
    spec parsing and `best_version` entirely.

    Returns a per-call summary in the same shape as `add()` so the
    caller can aggregate. Errors flow through host.record_failure with
    kinds drawn from FAILURE_KINDS.
    """
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")
    pkgs = _read_manifest(sd)
    summary = {"linked": [], "scripts": [], "missing": [], "conflicts": []}
    from .. import host

    spec_str = f"{pkg_name}=={version}@{wheel_tag}"
    conn = db.connect()
    try:
        if not store.has(conn, pkg_name, version, wheel_tag):
            summary["missing"].append(spec_str)
            host.record_failure(
                "shell_pkg_missing", spec_str,
                f"shell={name}; exact pin not in vault",
            )
            return summary
        vault_path = store.vault_path_for(pkg_name, version, wheel_tag)
        existing = pkgs.get(pkg_name)
        if existing and (existing["version"] != version
                         or existing["wheel_tag"] != wheel_tag):
            summary["conflicts"].append(
                (pkg_name, existing, {"version": version, "wheel_tag": wheel_tag})
            )
            host.record_failure(
                "shell_version_conflict", pkg_name,
                f"shell={name}; existing={existing}; requested="
                f"{{'version':'{version}','wheel_tag':'{wheel_tag}'}}",
            )
            return summary
        linked = _link_package(sd / "lib", Path(vault_path), pkg_name,
                               version=version, wheel_tag=wheel_tag)
        scripts = _link_entry_points(sd / "bin", Path(vault_path),
                                     python=str(sd / "python"))
        store.touch(pkg_name, version, wheel_tag)
        pkgs[pkg_name] = {"version": version, "wheel_tag": wheel_tag}
        summary["linked"].append((pkg_name, version, wheel_tag, len(linked)))
        summary["scripts"].extend(scripts)
        _write_manifest(sd, name, pkgs)
        conn.execute(
            "UPDATE shells SET last_used_at=? WHERE name=?",
            (datetime.now().isoformat(), name),
        )
        conn.commit()
    finally:
        conn.close()
    return summary


def remove_packages(name: str, pkgs: list[str]) -> list[str]:
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")
    manifest = _read_manifest(sd)
    removed = []
    for p in pkgs:
        if p not in manifest:
            continue
        # Unlink whatever we linked; we don't track which entries belonged to
        # which package, so re-derive from the vault path.
        info = manifest.pop(p)
        vault_path = Path(store.vault_path_for(p, info["version"], info["wheel_tag"]))
        if vault_path.exists():
            for entry in vault_path.iterdir():
                if entry.name.endswith(".dist-info"):
                    continue
                target = sd / "lib" / entry.name
                if target.is_symlink() and Path(os.readlink(target)) == entry:
                    target.unlink()
            for sn, _mod, _attr in _entry_points_for(vault_path):
                ep = sd / "bin" / sn
                if ep.exists():
                    ep.unlink()
        removed.append(p)
    _write_manifest(sd, name, manifest)
    return removed


def list_shells() -> list[dict]:
    conn = db.connect()
    rows = list(conn.execute(
        "SELECT name, created_at, last_used_at, shell_path, python_tag FROM shells"
    ))
    conn.close()
    out = []
    for name, created, used, path, py in rows:
        sd = Path(path)
        manifest = _read_manifest(sd) if sd.exists() else {}
        try:
            size = sum(f.stat().st_size for f in sd.rglob("*") if f.is_file())
        except OSError:
            size = 0
        out.append({
            "name": name,
            "created_at": created,
            "last_used_at": used,
            "path": path,
            "python_tag": py,
            "package_count": len(manifest),
            "size_bytes": size,
        })
    return out


def delete(name: str) -> bool:
    sd = shell_dir(name)
    conn = db.connect()
    conn.execute("DELETE FROM shells WHERE name=?", (name,))
    conn.commit()
    conn.close()
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
        return True
    return False


def exec_in(name: str, cmd: list[str]) -> int:
    """Run a command with the shell's PYTHONPATH/PATH set."""
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(sd / "lib") + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")
    env["PATH"] = str(sd / "bin") + ":" + env.get("PATH", "")
    env["BUBBLE_SHELL"] = name
    env["BUBBLE_SHELL_DIR"] = str(sd)

    # Update last_used
    conn = db.connect()
    conn.execute(
        "UPDATE shells SET last_used_at=? WHERE name=?",
        (datetime.now().isoformat(), name),
    )
    conn.commit()
    conn.close()
    return subprocess.call(cmd, env=env)


def discover_shell_for(start: Path) -> Optional[str]:
    """Walk up from `start` looking for a `.bubble-shell` file.

    The file's first non-comment line is the shell name. Returns None if
    none found (or the named shell doesn't exist).
    """
    here = start.resolve()
    while True:
        marker = here / ".bubble-shell"
        if marker.exists():
            for line in marker.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    if shell_dir(line).exists():
                        return line
                    return None
        if here.parent == here:
            return None
        here = here.parent
