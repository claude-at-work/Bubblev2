"""Project-level ingestion — scan a directory, create a named shell.

Pipeline
--------
1. Walk <project_dir>/**/*.py via scanner.scan_dir, merging all imports.
2. Resolve the non-stdlib, non-local import set against the vault
   (scanner.resolver.resolve).
3. Optionally fetch still-missing packages from PyPI (--fetch).
4. Create or update a named shell pinned to the resolved closure.
5. Save the shell scope to metadata so BUBBLE_SHELL-aware import
   resolution always enforces the project's exact versions.
6. Write a .bubble-shell marker in the project root so
   shell.discover_shell_for() can find the project automatically.

Spinoff / parent projects
-------------------------
Pass parent=<shell-name> to inherit packages that are not overridden
by this project's own resolution.  On import, the meta-finder walks the
parent chain (shell.load_parent) so a spinoff gets its parent's pinned
version for any package not in its own scope, while remaining free to
pin a different version for packages it does own.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def ingest(
    project_dir: Path,
    shell_name: str,
    *,
    fetch: bool = False,
    overwrite: bool = False,
    parent: Optional[str] = None,
    verbose: bool = False,
) -> dict:
    """Scan *project_dir*, resolve deps, create / update a named shell.

    Returns a summary dict::

        {
          "scanned_files": int,          # .py files successfully scanned
          "scan_errors":  [(path, msg)], # files that could not be parsed
          "resolved":     {dist: Resolved},
          "missing_imports": [str],      # distributions not found in vault
          "linked":    [(pkg, ver, tag, n)],
          "scripts":   [str],
          "missing":   [str],            # from add() — vault misses
          "conflicts": [(pkg, old, new)],
          "shell_dir": Path,
        }
    """
    from .scanner.py import scan_dir
    from .scanner.resolver import resolve, fetch_missing
    from .run import shell as shell_mod
    from .vault import db

    project_dir = Path(project_dir).resolve()
    if not project_dir.is_dir():
        raise NotADirectoryError(f"not a directory: {project_dir}")

    # 1. Scan
    merged, scan_errors = scan_dir(project_dir)
    if verbose:
        import sys
        print(f"  scanned {sum(1 for _ in project_dir.rglob('*.py'))} .py files, "
              f"{len(merged.top_level_imports)} external imports", file=sys.stderr)

    # 2. Resolve
    plan = resolve(merged)

    # 3. Optionally fetch missing
    if fetch and plan.missing:
        if verbose:
            import sys
            print(f"  fetching {len(plan.missing)} missing: "
                  f"{', '.join(sorted(plan.missing))}", file=sys.stderr)
        plan = fetch_missing(plan)

    db.init_db()

    # 4. Create or update shell
    sd = shell_mod.create(shell_name, [], exist_ok=overwrite, parent=parent)

    # Build pinned specs from the resolved plan and add them.
    specs = [
        f"{r.distribution}=={r.version}"
        for r in plan.resolved.values()
    ]
    if specs:
        summary = shell_mod.add(shell_name, specs)
    else:
        summary = {"linked": [], "scripts": [], "missing": [], "conflicts": []}

    # Scope is synced automatically by add(); nothing extra needed.

    # 5. Write .bubble-shell marker
    marker = project_dir / ".bubble-shell"
    if not marker.exists() or overwrite:
        marker.write_text(f"# bubble project shell\n{shell_name}\n")

    summary["scanned_files"] = sum(1 for _ in project_dir.rglob("*.py"))
    summary["scan_errors"] = [(str(p), msg) for p, msg in scan_errors]
    summary["resolved"] = plan.resolved
    summary["missing_imports"] = plan.missing
    summary["shell_dir"] = sd

    return summary


def freeze(shell_name: str, output: Path) -> None:
    """Write the current shell state as a deployment manifest.

    This is the inverse of `bubble shell create --from`: given a
    live shell, emit a manifest that can reproduce it elsewhere.
    Aliases stored in metadata are included.
    """
    from . import manifest as manifest_mod
    from .run import shell as shell_mod
    from .vault import db

    db.init_db()
    sd = shell_mod.shell_dir(shell_name)
    m = manifest_mod.from_shell(sd)

    # Re-attach aliases from metadata if present.
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT metadata FROM shells WHERE name=?", (shell_name,),
        ).fetchone()
    finally:
        conn.close()
    if row and row[0]:
        import json
        blob = json.loads(row[0])
        for alias, info in blob.get("aliases", {}).items():
            m.aliases[alias] = manifest_mod.AliasPin(
                name=info["name"],
                version=info["version"],
                wheel_tag=info["wheel_tag"],
                substrate=info.get("substrate"),
            )

    manifest_mod.dump(m, output)
