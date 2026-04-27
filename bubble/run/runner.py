"""Run a script inside an assembled bubble.

Stage 5:  run(env, cmd) -> ExitStatus

Error-driven retry remains for catching dynamic imports, but is now a
*secondary* path: the static scanner+resolver is primary. If the error loop
fires, that's a signal to add the missing module to the script's known
imports — we log the offending line.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .assemble import BubbleEnv
from ..vault import db, fetcher
from ..scanner import resolver as resolver_mod, py as scanner_py


_MODNF_RE = re.compile(r"No module named '([^']+)'")


def run(env: BubbleEnv, cmd: list[str], *,
        max_retries: int = 8, verbose: bool = False) -> int:
    """Execute cmd within env. On ModuleNotFoundError, vault-fetch and retry."""
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = env.pythonpath
    full_env["PATH"] = env.path
    full_env["BUBBLE_DIR"] = str(env.bubble_dir)

    retries = 0
    while True:
        proc = subprocess.run(cmd, env=full_env, capture_output=False)
        if proc.returncode == 0:
            return 0
        # Failure path — was it a missing module?
        if retries >= max_retries:
            return proc.returncode

        # Re-run capturing stderr to inspect
        proc = subprocess.run(cmd, env=full_env, capture_output=True, text=True)
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.returncode == 0:
            return 0
        m = _MODNF_RE.search(proc.stderr or "")
        if not m:
            sys.stderr.write(proc.stderr)
            return proc.returncode

        missing_import = m.group(1).split(".")[0]
        if verbose:
            print(f"  ⤷ dynamic import detected: {missing_import}", file=sys.stderr)
        dist_name = scanner_py.IMPORT_TO_DIST.get(missing_import, missing_import)
        from .. import host
        try:
            result = fetcher.fetch_into_vault(dist_name)
        except (ValueError, RuntimeError) as exc:
            host.record_failure("pypi_index_refused", dist_name,
                                f"{type(exc).__name__}: {exc}")
            sys.stderr.write(proc.stderr)
            sys.stderr.write(f"\n  could not fetch {dist_name}: {exc}\n")
            return proc.returncode
        except Exception as exc:
            host.record_failure("pypi_fetch_failed", dist_name,
                                f"{type(exc).__name__}: {exc}")
            sys.stderr.write(proc.stderr)
            sys.stderr.write(f"\n  could not fetch {dist_name}: {exc}\n")
            return proc.returncode
        if not result:
            host.record_failure("pypi_no_compatible_release", dist_name,
                                f"import_name={missing_import}")
            sys.stderr.write(proc.stderr)
            sys.stderr.write(f"\n  no compatible release for {dist_name}\n")
            return proc.returncode

        # Symlink the new package into the bubble
        from .assemble import assemble
        from ..scanner.py import scan as scan_script
        from ..scanner.resolver import resolve as resolve_imports
        # Re-scan + re-resolve + re-assemble (idempotent — existing symlinks stay)
        if len(cmd) >= 2 and Path(cmd[1]).exists() and Path(cmd[1]).suffix == ".py":
            iset = scan_script(Path(cmd[1]))
            iset.top_level_imports.add(missing_import)
        else:
            iset = scanner_py.ImportSet(script=Path(cmd[0]) if cmd else Path("."))
            iset.top_level_imports.add(missing_import)
        plan = resolve_imports(iset)
        if plan.missing:
            sys.stderr.write(proc.stderr)
            sys.stderr.write(f"\n  still missing after fetch: {plan.missing}\n")
            return proc.returncode
        assemble(plan, env.bubble_dir)
        retries += 1
