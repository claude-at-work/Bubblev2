"""Locate and parse project manifests to extract declared dependencies.

Supports (stdlib-only, no external parsers required):
  - pyproject.toml   PEP 621 [project] dependencies
                     Poetry  [tool.poetry.dependencies]
  - requirements.txt one PEP 508 spec per line
  - setup.cfg        [options] install_requires

The goal is narrow: give the fault loop a list of distribution names declared
by the current project so it can (a) do a one-shot prefetch of everything that
is not yet in the vault, and (b) resolve import names to their canonical dist
names more accurately than the static IMPORT_TO_DIST table.

Python 3.11+ has tomllib in stdlib; older versions fall back to a minimal
regex parser that handles the common [project] / [tool.poetry.*] structures.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# ──────────────────────────── manifest discovery ─────────────────────────────

_MANIFEST_NAMES = ("pyproject.toml", "requirements.txt", "setup.cfg")


def find_manifest(start: Path) -> Optional[Path]:
    """Walk up from *start* looking for a project manifest.

    Returns the first match found, or None. Stops at the filesystem root.
    Preference order within each directory: pyproject.toml > requirements.txt
    > setup.cfg.
    """
    here = start.resolve()
    # If start is a file, begin from its parent.
    if here.is_file():
        here = here.parent
    while True:
        for name in _MANIFEST_NAMES:
            candidate = here / name
            if candidate.is_file():
                return candidate
        parent = here.parent
        if parent == here:
            return None
        here = parent


# ──────────────────────────── dep-string helpers ─────────────────────────────

_DEP_NAME_RE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)")


def _dist_name(spec: str) -> str:
    """Extract the bare distribution name from a PEP 508 dependency string.

    Examples:
      "requests>=2.0,<3"   → "requests"
      "Pillow[jpeg]"        → "Pillow"
      "numpy ; python_version>='3.10'"  → "numpy"
      "git+https://…"       → ""  (VCS URLs — skip)
    """
    spec = spec.strip().strip('"').strip("'").strip()
    if not spec or spec.startswith("#") or spec.startswith("-"):
        return ""
    # VCS / URL references — not installable from PyPI
    if spec.startswith(("git+", "hg+", "svn+", "bzr+", "http://", "https://")):
        return ""
    m = _DEP_NAME_RE.match(spec)
    return m.group(1) if m else ""


# ──────────────────────────── format parsers ─────────────────────────────────


def _parse_pyproject(path: Path) -> list[str]:
    """Extract dist names from pyproject.toml.

    Tries tomllib (3.11+) first; falls back to a regex scanner that handles
    PEP 621's [project] dependencies array and Poetry's
    [tool.poetry.dependencies] table.
    """
    try:
        import tomllib  # type: ignore[import]
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return _extract_from_parsed_toml(data)
    except ImportError:
        pass

    # Regex fallback — handles the two common layouts.
    text = path.read_text(errors="replace")
    return _regex_parse_pyproject(text)


def _extract_from_parsed_toml(data: dict) -> list[str]:
    names: list[str] = []
    # PEP 621
    project = data.get("project") or {}
    for spec in project.get("dependencies") or []:
        n = _dist_name(str(spec))
        if n:
            names.append(n)
    # optional-dependencies (all extras)
    for _extra, specs in (project.get("optional-dependencies") or {}).items():
        for spec in specs or []:
            n = _dist_name(str(spec))
            if n:
                names.append(n)
    # Poetry
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for key, val in (poetry.get("dependencies") or {}).items():
        if key.lower() == "python":
            continue
        n = _dist_name(key)
        if n:
            names.append(n)
    return names


# Match the section header and collect everything until the next [section].
_SECTION_RE = re.compile(r"^\[([^\]]+)\]", re.MULTILINE)
# Single string on the same line:  dependencies = "foo"
_SINGLE_STR_RE = re.compile(r'^\s*dependencies\s*=\s*"([^"]+)"', re.MULTILINE)
# Array (possibly multi-line):  dependencies = [ ... ]
_ARRAY_RE = re.compile(
    r"^\s*dependencies\s*=\s*\[([^\]]*)\]", re.MULTILINE | re.DOTALL
)
_QUOTED_ITEM_RE = re.compile(r'["\']([^"\']+)["\']')


def _regex_parse_pyproject(text: str) -> list[str]:
    names: list[str] = []

    # Locate [project] and [tool.poetry.dependencies] sections.
    section_starts = [(m.group(1).strip(), m.start()) for m in _SECTION_RE.finditer(text)]
    sections: dict[str, str] = {}
    for i, (sec_name, start) in enumerate(section_starts):
        end = section_starts[i + 1][1] if i + 1 < len(section_starts) else len(text)
        sections[sec_name] = text[start:end]

    for sec_name, body in sections.items():
        if sec_name == "project":
            # Look for dependencies = [ ... ] or dependencies = "..."
            am = _ARRAY_RE.search(body)
            if am:
                for item in _QUOTED_ITEM_RE.findall(am.group(1)):
                    n = _dist_name(item)
                    if n:
                        names.append(n)
            else:
                sm = _SINGLE_STR_RE.search(body)
                if sm:
                    n = _dist_name(sm.group(1))
                    if n:
                        names.append(n)

        elif sec_name == "tool.poetry.dependencies":
            # key = "version" or key = { version = ..., ... }
            for line in body.splitlines():
                line = line.strip()
                if not line or line.startswith("[") or line.startswith("#"):
                    continue
                key = line.split("=")[0].strip()
                if key.lower() == "python":
                    continue
                n = _dist_name(key)
                if n:
                    names.append(n)

    return names


def _parse_requirements(path: Path) -> list[str]:
    """Extract dist names from requirements.txt.

    Skips comments, blank lines, -r/-c includes, and VCS/URL references.
    Does not recurse into included files — the goal is the declared direct
    deps, not the full transitive closure.
    """
    names: list[str] = []
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-r", "-c", "-e", "--")):
            continue
        n = _dist_name(line)
        if n:
            names.append(n)
    return names


def _parse_setup_cfg(path: Path) -> list[str]:
    """Extract dist names from setup.cfg [options] install_requires."""
    import configparser
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(path.read_text(errors="replace"))
    except configparser.Error:
        return []
    raw = cfg.get("options", "install_requires", fallback="")
    names: list[str] = []
    for line in raw.splitlines():
        n = _dist_name(line)
        if n:
            names.append(n)
    return names


# ──────────────────────────── public entry points ────────────────────────────


def parse_deps(manifest: Path) -> list[str]:
    """Return a list of bare distribution names declared in *manifest*.

    Duplicates are removed; order follows declaration order.  An empty list
    is returned on any parse error — the caller should proceed gracefully.
    """
    try:
        if manifest.name == "pyproject.toml":
            raw = _parse_pyproject(manifest)
        elif manifest.name == "requirements.txt":
            raw = _parse_requirements(manifest)
        elif manifest.name == "setup.cfg":
            raw = _parse_setup_cfg(manifest)
        else:
            return []
    except Exception:
        return []

    seen: set[str] = set()
    out: list[str] = []
    for name in raw:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def deps_not_in_vault(manifest: Path) -> list[str]:
    """Return declared deps that have no matching entry in the vault.

    These are the candidates the fault loop should pre-fetch before the first
    import attempt so that individual import faults don't drive repeated
    network round-trips.
    """
    from .vault import db, store
    declared = parse_deps(manifest)
    if not declared:
        return []
    db.init_db()
    conn = db.connect()
    try:
        missing: list[str] = []
        for dist in declared:
            rows = store.find_versions(conn, dist)
            if not rows:
                missing.append(dist)
        return missing
    finally:
        conn.close()
