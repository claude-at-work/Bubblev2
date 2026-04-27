"""Assemble a bubble from a ResolutionPlan.

Stage 4:  assemble(plan, target_dir) -> BubbleEnv
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..scanner.resolver import ResolutionPlan
from ..vault import store


@dataclass
class BubbleEnv:
    bubble_dir: Path
    lib: Path
    bin: Path
    pythonpath: str
    path: str
    extras: dict


def assemble(plan: ResolutionPlan, target_dir: Path) -> BubbleEnv:
    """Build the bubble directory tree by symlinking each resolved package's
    importable entries into target_dir/lib/.

    Whole-package symlinks (data files come along). Per-module is a tunable
    we don't need yet — the original bubble's per-module assembly was an
    optimization that broke pkg/data/*.json.
    """
    target_dir = Path(target_dir)
    lib = target_dir / "lib"
    bin_ = target_dir / "bin"
    lib.mkdir(parents=True, exist_ok=True)
    bin_.mkdir(parents=True, exist_ok=True)

    # Resolve once, then check each resolved.vault_path against it. Avoids a
    # syscall per package on top of the iterdir() we'd be doing anyway.
    from .. import config
    vault_root = config.VAULT_DIR.resolve()
    for resolved in plan.resolved.values():
        rp = Path(resolved.vault_path).resolve()
        if rp != vault_root and vault_root not in rp.parents:
            raise ValueError(
                f"refusing to assemble from outside the vault: {resolved.vault_path}"
            )
        for entry in resolved.vault_path.iterdir():
            if entry.name.endswith(".dist-info") or entry.name.endswith(".data"):
                continue
            dest = lib / entry.name
            if dest.exists() or dest.is_symlink():
                continue
            try:
                os.symlink(entry, dest)
            except OSError:
                if entry.is_dir():
                    shutil.copytree(entry, dest)
                else:
                    shutil.copy2(entry, dest)
        # touch last_used_at for GC
        store.touch(resolved.distribution, resolved.version, resolved.wheel_tag)

    pythonpath = str(lib)
    if "PYTHONPATH" in os.environ:
        pythonpath = pythonpath + ":" + os.environ["PYTHONPATH"]
    path = str(bin_) + ":" + os.environ.get("PATH", "")
    return BubbleEnv(
        bubble_dir=target_dir,
        lib=lib,
        bin=bin_,
        pythonpath=pythonpath,
        path=path,
        extras={},
    )
