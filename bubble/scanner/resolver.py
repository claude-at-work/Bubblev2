"""Resolve an ImportSet against the vault, fetching missing pieces.

Stage 2 + 3:  resolve(ImportSet) -> ResolutionPlan
              fetch(ResolutionPlan) -> ResolutionPlan with missing filled
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .py import ImportSet, IMPORT_TO_DIST
from ..vault import db, store
from ..run.shell import best_version  # version+wheel_tag picker


@dataclass
class Resolved:
    distribution: str       # PyPI name
    version: str
    wheel_tag: str
    vault_path: Path


@dataclass
class ResolutionPlan:
    imports: ImportSet
    resolved: dict[str, Resolved] = field(default_factory=dict)   # by distribution name
    missing: list[str] = field(default_factory=list)              # distribution names

    @property
    def vault_paths(self) -> list[Path]:
        return [r.vault_path for r in self.resolved.values()]


def resolve(imports: ImportSet) -> ResolutionPlan:
    """Match each top-level import against the vault. No network calls."""
    plan = ResolutionPlan(imports=imports)
    conn = db.connect()
    try:
        for dist in sorted(imports.candidate_distributions):
            picked = best_version(conn, dist, pinned_version=None)
            if not picked:
                # Try the original import-name in case IMPORT_TO_DIST got it wrong
                inverse = next((k for k, v in IMPORT_TO_DIST.items() if v == dist), None)
                if inverse:
                    picked = best_version(conn, inverse, pinned_version=None)
                if not picked:
                    plan.missing.append(dist)
                    continue
                dist = inverse
            version, wheel_tag, vault_path = picked
            plan.resolved[dist] = Resolved(
                distribution=dist, version=version, wheel_tag=wheel_tag,
                vault_path=Path(vault_path),
            )
    finally:
        conn.close()
    return plan


def fetch_missing(plan: ResolutionPlan, *, allow_prerelease: bool = False) -> ResolutionPlan:
    """Try to pull every plan.missing entry from PyPI into the vault, then re-resolve."""
    if not plan.missing:
        return plan
    from ..vault import fetcher
    still_missing = []
    for dist in list(plan.missing):
        try:
            result = fetcher.fetch_into_vault(dist, allow_prerelease=allow_prerelease)
        except Exception:
            still_missing.append(dist)
            continue
        if not result:
            still_missing.append(dist)
    plan.missing = still_missing
    # Re-resolve
    return resolve(plan.imports)
