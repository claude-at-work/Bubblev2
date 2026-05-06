"""doctor — one-screen environment health check.

Adapted from legacy/bubble.py:1888-1983, integrated with the current
architecture: vault stats from store, host portrait from host.toml,
shims from shims module, shell count from shell module.

Read-only. The companion `bubble probe` writes host.toml; this command
reads it. If the portrait isn't there, doctor still works — it just
notes the gap.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import config, host, shims, term
from .vault import db
from .run import shell as shell_mod


def _vault_stats() -> dict:
    if not config.VAULT_DB.exists():
        return {"packages": 0, "native": 0, "modules": 0, "top_level": 0}
    conn = db.connect()
    try:
        n_pkgs = conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
        n_native = conn.execute(
            "SELECT COUNT(*) FROM packages WHERE has_native=1"
        ).fetchone()[0]
        n_mods = conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0]
        n_tl = conn.execute("SELECT COUNT(*) FROM top_level").fetchone()[0]
    finally:
        conn.close()
    return {"packages": n_pkgs, "native": n_native,
            "modules": n_mods, "top_level": n_tl}


def _vault_size_bytes() -> int:
    """Disk usage of the vault tree, deduplicated by inode.

    The vault hardlinks heavily — without dedup, a logical sum overcounts
    by however many hardlinks each blob has, painting a "vault is
    bloated" picture that the underlying filesystem doesn't actually own.
    """
    if not config.VAULT_DIR.exists():
        return 0
    seen: set[tuple[int, int]] = set()
    total = 0
    for f in config.VAULT_DIR.rglob("*"):
        try:
            if not f.is_file() or f.is_symlink():
                continue
            st = f.stat()
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
        except OSError:
            continue
    return total


def _symlinks_supported() -> bool:
    config.BUBBLE_HOME.mkdir(parents=True, exist_ok=True)
    target = config.BUBBLE_HOME / ".symlink_target"
    link = config.BUBBLE_HOME / ".symlink_test"
    try:
        target.touch()
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        link.unlink()
        target.unlink()
        return True
    except OSError:
        for p in (link, target):
            try: p.unlink()
            except OSError: pass
        return False


def _which_version(cmd: str, flag: str = "--version") -> Optional[str]:
    if not shutil.which(cmd):
        return None
    try:
        r = subprocess.run([cmd, flag], capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    text = (r.stdout or r.stderr).strip().splitlines()
    return text[0] if text else None


def _shell_count() -> int:
    if not config.VAULT_DB.exists():
        return 0
    try:
        return len(shell_mod.list_shells())
    except Exception:
        return 0


def run() -> int:
    """Print the health tree. Returns 0 — diagnosis, not gate."""
    stats = _vault_stats()
    size_mb = _vault_size_bytes() / (1024 * 1024)
    portrait = host.load()
    shim_rpt = shims.discover()

    term.out()
    term.out(f"  {term.bold('┌─ Bubble Doctor')}")
    term.out(f"  │")

    # ── platform / runtime ───────────────────────────────
    term.out(f"  ├─ Python:      {sys.version.split()[0]}  {term.dim(sys.executable)}")
    term.out(f"  ├─ Platform:    {platform.machine()} / {platform.system()}")
    term.out(f"  ├─ Host:        {config.detect_host()}")
    term.out(f"  │")

    # ── vault ────────────────────────────────────────────
    if stats["packages"] == 0:
        term.out(f"  ├─ Vault:       {term.dim('empty')}  "
                 f"{term.dim('(run `bubble setup` to import existing site-packages)')}")
    else:
        pure = stats["packages"] - stats["native"]
        n_native = stats["native"]
        n_mods = stats["modules"]
        n_tl = stats["top_level"]
        term.out(f"  ├─ Vault:       {stats['packages']} packages  "
                 f"{term.dim(f'({pure} pure, {n_native} native)')}")
        term.out(f"  │               {n_mods:,} modules, "
                 f"{n_tl:,} import names indexed")
    term.out(f"  ├─ Shells:      {_shell_count()}")
    term.out(f"  ├─ Vault size:  {size_mb:.1f} MB")
    term.out(f"  ├─ Symlinks:    "
             f"{term.green('✓ supported') if _symlinks_supported() else term.amber('⚠ not supported (will copy instead)')}")

    if stats["native"] > 0:
        term.out(f"  │")
        term.out(f"  ├─ {term.amber('⚠')} {stats['native']} native packages are arch-bound  "
                 f"{term.dim(f'(compiled for {platform.machine()})')}")
        term.out(f"  │   {term.dim('On different hardware they will need re-resolving from source.')}")

    # ── tools available on host ──────────────────────────
    term.out(f"  │")
    pip_v = _which_version("pip3") or _which_version("pip")
    term.out(f"  ├─ pip:         "
             f"{pip_v or term.red('✗ not available')}")
    node_v = _which_version("node")
    term.out(f"  ├─ node:        "
             f"{node_v or term.dim('— not installed')}")
    npm_v = _which_version("npm")
    term.out(f"  ├─ npm:         "
             f"{npm_v or term.dim('— not installed')}")

    # ── shims ────────────────────────────────────────────
    term.out(f"  │")
    if shim_rpt.cert_file:
        term.out(f"  ├─ SSL certs:   {term.green('✓')}  {term.dim(str(shim_rpt.cert_file))}")
    else:
        term.out(f"  ├─ SSL certs:   {term.amber('⚠ not found')}  "
                 f"{term.dim('(HTTPS may fail; pip install certifi as a fallback)')}")
    if shim_rpt.resolv_conf:
        term.out(f"  ├─ resolv.conf: {term.green('✓')}  {term.dim(str(shim_rpt.resolv_conf))}")
    else:
        term.out(f"  ├─ resolv.conf: {term.amber('⚠ not found')}")

    # ── substrates from host.toml ────────────────────────
    term.out(f"  │")
    if not portrait:
        term.out(f"  ├─ Substrates:  {term.dim('— host.toml not written')}  "
                 f"{term.dim('(run `bubble probe`)')}")
    else:
        substrates = portrait.get("substrates", [])
        term.out(f"  ├─ Substrates:  ({len(substrates)})")
        for s in substrates:
            name = s.get("name", "?")
            status = s.get("status", "")
            cost = f"~{s['cost_mb']}MB" if s.get("cost_mb") else "n/a"
            mark = term.green("✓") if str(status).startswith("available") else term.dim("○")
            term.out(f"  │     {mark} {name:<22} {term.dim(f'cost={cost:<8}')} {term.dim(status)}")

    # ── recorded failures ────────────────────────────────
    if portrait:
        failures = portrait.get("failures", [])
        term.out(f"  │")
        if not failures:
            term.out(f"  ├─ Failures:    {term.green('✓')} none recorded")
        else:
            term.out(f"  ├─ Failures:    {term.amber('⚠')} {len(failures)} recorded  "
                     f"{term.dim('(`bubble host` for detail)')}")
            for f in failures[-3:]:
                term.out(f"  │     {term.dim('×')} [{f.get('kind','?')}] {f.get('target','?')}")

    term.out(f"  │")
    term.out(f"  └─ Home:        {term.dim(str(config.BUBBLE_HOME))}")
    term.out()
    return 0
