"""preflight — given a script, produce an offline-readiness shopping list.

Adapted from legacy/bubble.py:1986-2076. Reuses the scanner + resolver to
match imports against the vault, then walks the dependencies table to
catch transitive gaps. Closes with a one-line ready-or-not summary.

The exact `bubble vault get` commands are surfaced verbatim so you can
copy-paste your way to offline-ready.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from . import config, shims, term
from .scanner import py as scanner_py, resolver as resolver_mod
from .vault import db, metadata as vault_metadata


_STDLIB: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())


def _vault_size_kb(name: str, version: str, wheel_tag: str) -> float:
    if not config.VAULT_DB.exists():
        return 0.0
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM vault_files "
            "WHERE package=? AND version=? AND wheel_tag=?",
            (name, version, wheel_tag),
        ).fetchone()
    finally:
        conn.close()
    return (int(row[0]) if row else 0) / 1024.0


def _has_native(name: str, version: str, wheel_tag: str) -> bool:
    if not config.VAULT_DB.exists():
        return False
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT has_native FROM packages WHERE name=? AND version=? AND wheel_tag=?",
            (name, version, wheel_tag),
        ).fetchone()
    finally:
        conn.close()
    return bool(row[0]) if row else False


def _transitive_missing(resolved_names: set[str]) -> list[tuple[str, str]]:
    """For each resolved package, walk its non-optional Requires-Dist; report
    (parent, dep) pairs where dep isn't in the vault and isn't a sibling
    in the same plan.

    Two normalizations rule out false positives the user would otherwise see:

    - **PEP 503 name normalization.** The dependencies table stores whatever
      METADATA wrote ('charset-normalizer'); the packages table can hold any
      historical case ('charset_normalizer', 'Charset-Normalizer'). Compare
      both via vault_metadata.normalize_name so the lookup matches reality.

    - **Stdlib backport-name heuristic.** Some packages list dependencies
      whose distribution name coincides with a stdlib module — `dataclasses`,
      `typing`, `ipaddress`. Those Requires-Dist lines are guarded by
      python_version markers we don't fully evaluate (PEP 508, README
      limitation). On a Python where the stdlib has the module, the dep is
      satisfied by the runtime and the user shouldn't see ⚠.
    """
    if not config.VAULT_DB.exists() or not resolved_names:
        return []
    norm = vault_metadata.normalize_name
    resolved_norm = {norm(n) for n in resolved_names}
    out: list[tuple[str, str]] = []
    conn = db.connect()
    try:
        rows = conn.execute("SELECT DISTINCT name FROM packages").fetchall()
        in_vault_norm = {norm(r[0]) for r in rows}
        for parent in sorted(resolved_names):
            rows = conn.execute(
                "SELECT DISTINCT dep_name FROM dependencies "
                "WHERE package=? AND optional=0",
                (parent,),
            ).fetchall()
            for (dep_name,) in rows:
                if dep_name in _STDLIB:
                    continue
                d_norm = norm(dep_name)
                if d_norm in resolved_norm or d_norm in in_vault_norm:
                    continue
                out.append((parent, dep_name))
    finally:
        conn.close()
    return out


def run(script_path: Path) -> int:
    """Print the preflight tree. Returns 0 if ready offline, 1 otherwise."""
    script_path = Path(script_path).resolve()
    if not script_path.exists():
        term.err(f"  {term.red('✗')} not found: {script_path}")
        return 1

    try:
        iset = scanner_py.scan(script_path)
    except ValueError as exc:
        term.err(f"  {term.red('✗')} could not scan: {exc}")
        return 1

    plan: Optional[resolver_mod.ResolutionPlan] = None
    if config.VAULT_DB.exists():
        plan = resolver_mod.resolve(iset)

    term.out()
    term.out(f"  {term.bold(f'┌─ Preflight: {script_path.name}')}  {term.dim('[py]')}")
    term.out(f"  │")

    if plan is None:
        term.out(f"  ├─ {term.amber('⚠')} vault not initialized  "
                 f"{term.dim('(run `bubble setup`)')}")
        missing = sorted(iset.candidate_distributions)
    else:
        missing = sorted(plan.missing)
        if plan.resolved:
            term.out(f"  ├─ {term.green('✓')} Ready ({len(plan.resolved)}):")
            for dist in sorted(plan.resolved):
                r = plan.resolved[dist]
                kb = _vault_size_kb(dist, r.version, r.wheel_tag)
                native = term.dim(" [native]") if _has_native(dist, r.version, r.wheel_tag) else ""
                term.out(f"  │     {dist}=={r.version}  "
                         f"{term.dim(f'({kb:.1f}KB)')}{native}")

    if missing:
        term.out(f"  │")
        term.out(f"  ├─ {term.red('✗')} Need to vault ({len(missing)}):")
        for pkg in missing:
            term.out(f"  │     {term.dim('$')} bubble vault get {pkg}")

    transitive: list[tuple[str, str]] = []
    if plan and plan.resolved:
        transitive = _transitive_missing(set(plan.resolved.keys()))
    if transitive:
        term.out(f"  │")
        term.out(f"  ├─ {term.amber('⚠')} Transitive deps missing:")
        for parent, dep in transitive:
            term.out(f"  │     {dep}  {term.dim(f'(needed by {parent})')}")

    shim_rpt = shims.discover()
    if shim_rpt.gaps:
        term.out(f"  │")
        term.out(f"  ├─ {term.amber('⚠')} Shims unresolvable:")
        for gap in shim_rpt.gaps:
            term.out(f"  │     {gap}")

    total_blocking = len(missing) + len(transitive)

    term.out(f"  │")
    if total_blocking == 0 and not shim_rpt.gaps:
        term.out(f"  └─ {term.green('✓')} Ready for offline operation")
        term.out()
        return 0

    parts = []
    if missing:
        parts.append(term.amber(f"{len(missing)} packages"))
    if transitive:
        parts.append(term.amber(f"{len(transitive)} transitive"))
    if shim_rpt.gaps:
        parts.append(term.amber(f"{len(shim_rpt.gaps)} shims"))
    term.out(f"  └─ {', '.join(parts)} to resolve before going offline")
    term.out()
    return 1 if total_blocking else 0
