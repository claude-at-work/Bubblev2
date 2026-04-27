"""Bridge runner: route scripts across main bubble + legacy bubble safely."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_ALLOWED_LEGACY_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".tsx"}


def _resolve_script(script: str) -> Path:
    p = Path(script).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"script not found: {p}")
    if not p.is_file():
        raise ValueError(f"script must be a regular file: {p}")
    if p.is_symlink():
        raise ValueError(f"symlinked script paths are refused: {p}")
    return p


def _python_cmd(script: Path, args: list[str], *, fetch: bool, isolate: bool) -> list[str]:
    cmd = [sys.executable, "-m", "bubble", "run", str(script)]
    if fetch:
        cmd.append("--fetch")
    if isolate:
        cmd.append("--isolate")
    return cmd + list(args)


def _legacy_cmd(script: Path, args: list[str], *, keep: bool) -> list[str]:
    legacy_entry = Path(__file__).resolve().parents[1] / "legacy" / "bubble.py"
    cmd = [sys.executable, str(legacy_entry), "up", str(script)]
    if keep:
        cmd.append("--keep")
    return cmd + list(args)


def _hardened_env(base: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if base is None else base
    env = {
        "HOME": src.get("HOME", str(Path.home())),
        "PATH": src.get("PATH", "/usr/bin:/bin"),
        "TMPDIR": src.get("TMPDIR", "/tmp"),
        "LANG": src.get("LANG", "C.UTF-8"),
        "LC_ALL": src.get("LC_ALL", "C.UTF-8"),
        "BUBBLE_HOME": src.get("BUBBLE_HOME", str(Path.home() / ".bubble")),
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "BUBBLE_BRIDGE_MODE": "1",
    }
    return env


def run(args: argparse.Namespace) -> int:
    script = _resolve_script(args.script)
    suffix = script.suffix.lower()

    if suffix == ".py":
        cmd = _python_cmd(script, args.args, fetch=args.fetch, isolate=not args.no_isolate)
    elif suffix in _ALLOWED_LEGACY_SUFFIXES:
        if not args.allow_legacy_network:
            print(
                "refusing legacy auto-fetch by default; run once with --allow-legacy-network "
                "to authorize network for this execution",
                file=sys.stderr,
            )
            return 2
        cmd = _legacy_cmd(script, args.args, keep=args.keep)
    else:
        allowed = ", ".join(sorted(_ALLOWED_LEGACY_SUFFIXES | {".py"}))
        print(f"unsupported script type '{suffix or '<none>'}'; supported: {allowed}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(" ".join(cmd))
        return 0

    env = _hardened_env()
    proc = subprocess.run(cmd, env=env)
    return proc.returncode
