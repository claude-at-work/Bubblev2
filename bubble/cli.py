"""bubble CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config
from . import bridge
from .vault import db, store, importer
from .run import shell as shell_mod


# ─────────────────────────── command handlers ────────────────────────────


def cmd_vault_list(args: argparse.Namespace) -> int:
    db.init_db()
    rows = store.list_all()
    if not rows:
        print("vault is empty")
        return 0
    print(f"{'NAME':<28} {'VERSION':<14} {'TAG':<46} {'TYPE':<6} {'SOURCE':<14}")
    print("─" * 110)
    for name, version, tag, native, source, _, _ in rows:
        ptype = "native" if native else "pure"
        print(f"{name:<28} {version:<14} {tag:<46} {ptype:<6} {source or '':<14}")
    print(f"\n{len(rows)} packages")
    return 0


def cmd_vault_import_venv(args: argparse.Namespace) -> int:
    db.init_db()
    site_packages = Path(args.path).resolve()
    if site_packages.is_file():
        print(f"error: {site_packages} is a file; pass a site-packages directory", file=sys.stderr)
        return 1
    # If user passed a venv root, drill in
    if not (site_packages.name == "site-packages" or any(
            site_packages.glob("*.dist-info"))):
        candidates = list(site_packages.glob("lib/python*/site-packages"))
        if candidates:
            site_packages = candidates[0]
            print(f"using {site_packages}")
    print(f"importing {site_packages}")
    skip = set(args.skip or [])
    result = importer.import_site_packages(
        site_packages, hardlink=args.hardlink, overwrite=args.overwrite, skip=skip,
    )
    print(f"\n  imported:       {result['imported']}")
    print(f"  skipped:        {result['skipped']}  {result.get('skipped_names', [])[:8]}")
    print(f"  missing RECORD: {result['missing_record']}")
    print(f"  errors:         {result['errors']}")
    if args.verbose:
        for entry in result["entries"]:
            print(" ", entry)
    return 0 if result["errors"] == 0 else 2


def cmd_vault_audit_fs(args: argparse.Namespace) -> int:
    db.init_db()
    root = Path(args.root).resolve()
    print(f"scanning {root} for venvs...")
    venvs = []
    for marker in root.rglob("pyvenv.cfg"):
        venv = marker.parent
        sps = list(venv.glob("lib/python*/site-packages"))
        if not sps:
            continue
        sp = sps[0]
        try:
            size = sum(f.stat().st_size for f in sp.rglob("*") if f.is_file())
        except OSError:
            size = 0
        venvs.append((venv, sp, size))

    if not venvs:
        print("no venvs found")
        return 0

    pkg_locations: dict[str, list[tuple[str, str, Path]]] = {}
    from .vault.metadata import name_version_from_dist_info, derive_wheel_tag_from_dist_info
    for venv, sp, _size in venvs:
        for di in sp.glob("*.dist-info"):
            nv = name_version_from_dist_info(di)
            if not nv:
                continue
            name, version = nv
            tag, *_ = derive_wheel_tag_from_dist_info(di)
            pkg_locations.setdefault(name.lower(), []).append((version, tag, di))

    # Cross-check what's already in vault
    conn = db.connect()
    in_vault = {(name.lower(), version, tag): vault_path
                for name, version, tag, _, _, _, vault_path in store.list_all(conn)}
    conn.close()

    print(f"\n=== venvs ({len(venvs)}) ===")
    total = 0
    for venv, sp, size in sorted(venvs, key=lambda x: -x[2]):
        total += size
        print(f"  {size/1e6:>8.1f}MB  {venv}")
    print(f"\n  total site-packages: {total/1e6:.1f}MB")

    dup_waste = 0
    print(f"\n=== duplicates ({sum(1 for v in pkg_locations.values() if len(v) > 1)}) ===")
    for name, locs in sorted(pkg_locations.items()):
        if len(locs) <= 1:
            continue
        # Compute waste: total RECORD-listed bytes minus largest single
        sizes = []
        for ver, tag, di in locs:
            try:
                bytes_ = sum((di.parent / Path(row.split(",")[0])).stat().st_size
                             for row in (di / "RECORD").read_text().splitlines()
                             if row and ".." not in row.split(",")[0])
            except (OSError, IndexError):
                bytes_ = 0
            sizes.append((ver, tag, bytes_, di))
        waste = (sum(s[2] for s in sizes) - max(s[2] for s in sizes))
        dup_waste += waste
        print(f"  {name}: {len(locs)} copies, dup_waste={waste/1e6:.1f}MB")
        for ver, tag, b, di in sizes:
            in_v = "✓" if (name, ver, tag) in in_vault else " "
            print(f"      {in_v} {ver:<14} {tag:<32} {b/1e6:>6.1f}MB  {di.parent}")

    print(f"\n  total dup waste: {dup_waste/1e6:.1f}MB")

    safely_removable = 0
    print(f"\n=== already in vault (safely removable from venv) ===")
    for name, locs in sorted(pkg_locations.items()):
        for ver, tag, di in locs:
            if (name, ver, tag) in in_vault:
                safely_removable += 1
                print(f"  {name:<28} {ver:<14} {tag:<32}  {di.parent}")
    print(f"\n  {safely_removable} dist-info entries safely removable\n")
    return 0


def cmd_vault_remove(args: argparse.Namespace) -> int:
    db.init_db()
    ok = store.remove(args.name, args.version, args.tag)
    print("removed" if ok else "not found")
    return 0 if ok else 1


def cmd_shell_create(args: argparse.Namespace) -> int:
    db.init_db()
    if args.from_manifest:
        return _shell_create_from_manifest(args)
    sd = shell_mod.create(args.name, args.specs or [], exist_ok=args.exist_ok)
    print(f"created shell '{args.name}' at {sd}")
    if args.specs:
        print(f"  added: {len(args.specs)} package specs")
    return 0


def _shell_create_from_manifest(args: argparse.Namespace) -> int:
    """Read a deployment manifest, fetch any missing pins (when --fetch
    is set), and link the shell. Each pin verifies through C1's
    verify-on-link before becoming an entry."""
    from . import manifest as manifest_mod
    manifest_path = Path(args.from_manifest).resolve()
    if not manifest_path.exists():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 1
    m = manifest_mod.load(manifest_path)

    if not m.packages and not m.aliases:
        print(f"error: manifest is empty (no [packages] or [aliases])",
              file=sys.stderr)
        return 1

    autofetch = bool(args.fetch) or bool(os.environ.get("BUBBLE_AUTOFETCH"))
    if autofetch:
        from .vault import fetcher
        for pkg, (version, wheel_tag) in m.packages.items():
            conn = db.connect()
            present = store.has(conn, pkg, version, wheel_tag)
            conn.close()
            if present:
                continue
            print(f"  fetching {pkg}=={version} ({wheel_tag}) ...")
            try:
                fetcher.fetch_into_vault(pkg, pinned_version=version)
            except (RuntimeError, ValueError) as exc:
                print(f"  fetch refused: {exc}", file=sys.stderr)
                return 2

    sd = shell_mod.create(args.name, [], exist_ok=args.exist_ok)
    if args.name == m.name or m.name is None:
        pass  # name agrees or unspecified
    elif m.name and m.name != args.name:
        print(f"  note: manifest names shell {m.name!r}; using CLI arg {args.name!r}",
              file=sys.stderr)

    total_summary = {"linked": [], "scripts": [], "missing": [], "conflicts": []}
    for pkg, (version, wheel_tag) in m.packages.items():
        s = shell_mod.add_pinned(args.name, pkg, version, wheel_tag)
        for k in total_summary:
            total_summary[k].extend(s[k])

    # Aliases: record into the shell-state manifest's metadata so future
    # lookups (and the substrate-routing thread, when it lands) can read
    # them. We store them under the shell row's metadata JSON for now —
    # the alias→substrate routing thread is where they become
    # operational.
    if m.aliases:
        conn = db.connect()
        row = conn.execute(
            "SELECT metadata FROM shells WHERE name=?", (args.name,),
        ).fetchone()
        meta_blob = json.loads(row[0]) if row and row[0] else {}
        meta_blob["aliases"] = {
            alias: {
                "name": pin.name,
                "version": pin.version,
                "wheel_tag": pin.wheel_tag,
                "substrate": pin.substrate,
            } for alias, pin in m.aliases.items()
        }
        conn.execute(
            "UPDATE shells SET metadata=? WHERE name=?",
            (json.dumps(meta_blob), args.name),
        )
        conn.commit()
        conn.close()

    print(f"created shell '{args.name}' at {sd}")
    print(f"  manifest: {manifest_path}")
    print(f"  linked:    {len(total_summary['linked'])}")
    if total_summary["missing"]:
        print(f"  MISSING:   {len(total_summary['missing'])} "
              f"(rerun with --fetch to pull from PyPI, or use "
              f"`bubble vault import-venv` to populate from a trusted venv)")
        for s in total_summary["missing"]:
            print(f"    × {s}")
    if total_summary["conflicts"]:
        print(f"  CONFLICTS: {len(total_summary['conflicts'])}")
        for pkg, existing, requested in total_summary["conflicts"]:
            print(f"    × {pkg}: shell pinned to {existing}, manifest asks {requested}")
    if m.aliases:
        print(f"  aliases:   {len(m.aliases)} recorded "
              f"(substrate routing not yet wired; see `bubble host`)")
    if total_summary["missing"] or total_summary["conflicts"]:
        return 2
    return 0


def cmd_shell_add(args: argparse.Namespace) -> int:
    db.init_db()
    summary = shell_mod.add(args.name, args.specs)
    for pkg, ver, tag, n in summary["linked"]:
        print(f"  + {pkg:<28} {ver:<14} {tag:<32}  ({n} entries)")
    if summary["scripts"]:
        print(f"  scripts: {', '.join(summary['scripts'])}")
    if summary["missing"]:
        print(f"  MISSING from vault: {', '.join(summary['missing'])}")
        return 2
    if summary["conflicts"]:
        for pkg, existing, new in summary["conflicts"]:
            print(f"  CONFLICT {pkg}: shell has {existing}, requested {new}")
        return 3
    return 0


def cmd_shell_remove(args: argparse.Namespace) -> int:
    db.init_db()
    removed = shell_mod.remove_packages(args.name, args.pkgs)
    print(f"unlinked: {', '.join(removed) if removed else '(nothing)'}")
    return 0


def cmd_shell_list(args: argparse.Namespace) -> int:
    db.init_db()
    shells = shell_mod.list_shells()
    if not shells:
        print("no shells")
        return 0
    print(f"{'NAME':<20} {'PYTHON':<8} {'PKGS':<6} {'SIZE':<10} {'PATH'}")
    print("─" * 90)
    for s in shells:
        print(f"{s['name']:<20} {s['python_tag'] or '':<8} {s['package_count']:<6} "
              f"{s['size_bytes']/1024:>8.1f}KB  {s['path']}")
    return 0


def cmd_shell_delete(args: argparse.Namespace) -> int:
    db.init_db()
    ok = shell_mod.delete(args.name)
    print("deleted" if ok else "not found")
    return 0 if ok else 1


def cmd_shell_exec(args: argparse.Namespace) -> int:
    db.init_db()
    return shell_mod.exec_in(args.name, args.cmd)


def cmd_host(args: argparse.Namespace) -> int:
    """bubble host — show what bubble knows about this machine.

    The consult side of the probe→consult→record loop. Reads ~/.bubble/host.toml
    that the probe wrote, plus any failures recorded by the runtime since.
    """
    from . import host
    portrait = host.load()
    if not portrait:
        print("no host portrait yet — run `bubble probe` to write one")
        return 1

    print(f"  probed_at:      {portrait.get('probed_at', '-')}")
    print(f"  bubble_version: {portrait.get('bubble_version', '-')}")
    if "kernel" in portrait:
        k = portrait["kernel"]
        print(f"  kernel:         {k.get('system','?')} {k.get('release','?')} {k.get('machine','?')}")
    if "libc" in portrait:
        l = portrait["libc"]
        print(f"  libc:           {l.get('variant','?')} {l.get('version','')}")
    print()
    print("  substrates:")
    for s in portrait.get("substrates", []):
        cost = f"~{s['cost_mb']}MB" if s.get("cost_mb") else "n/a"
        print(f"    • {s.get('name',''):<22} cost={cost:<8} {s.get('status','')}")
    failures = portrait.get("failures", [])
    if failures:
        print()
        print(f"  observed failures ({len(failures)}):")
        for f in failures[-10:]:  # last 10
            print(f"    × [{f.get('kind','?')}] {f.get('target','?')}")
            if f.get("detail"):
                print(f"      {f['detail'][:140]}")
    else:
        print()
        print("  no runtime failures recorded yet")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """bubble setup — zero-flag bootstrap.

    Probes the host, scans every site-packages this Python install knows
    about, imports everything into the vault (hardlink by default, falling
    back to copy on cross-fs). Idempotent: re-running after `pip install`
    only adds the new entries.
    """
    from . import probe
    import site as _site
    import sys as _sys
    config.ensure_dirs()
    db.init_db()

    # 1. Probe — host portrait + substrate menu.
    print("probing host...")
    results = probe.run_all()
    out_path = probe.host_toml_path()
    probe.write(out_path, results)
    py = results["python"]
    k = results["kernel"]
    print(f"  python {py['version']}  {k['system']} {k['machine']}")
    avail = [s["name"] for s in results["substrates"]
             if s.get("status", "").startswith("available")]
    if avail:
        print(f"  substrates: {', '.join(avail)}")

    # 2. Discover every site-packages on this interpreter's view.
    candidates: set[Path] = set()
    for p in _sys.path:
        if "packages" in p:
            pp = Path(p)
            if pp.is_dir():
                candidates.add(pp.resolve())
    for p in _site.getsitepackages():
        pp = Path(p)
        if pp.is_dir():
            candidates.add(pp.resolve())

    # Don't recurse into our own vault tree.
    bubble_home = Path(config.BUBBLE_HOME).resolve()
    candidates = {p for p in candidates
                  if bubble_home not in p.parents and p != bubble_home}

    if not candidates:
        print("\nno site-packages directories on this interpreter's path.")
        print("nothing to scan.  vault is ready at", bubble_home)
        return 0

    # 3. Import each, hardlinking by default (falls back to copy on EXDEV).
    print(f"\nscanning {len(candidates)} site-packages director"
          f"{'y' if len(candidates) == 1 else 'ies'}...")
    totals = {"imported": 0, "skipped": 0, "errors": 0, "missing_record": 0}
    for sp in sorted(candidates):
        n_dists = len(list(sp.glob("*.dist-info")))
        print(f"  {sp}  ({n_dists} dists)")
        r = importer.import_site_packages(sp, hardlink=True, overwrite=False)
        for k_ in totals:
            totals[k_] += r.get(k_, 0)

    # 4. Report what's ready.
    print(f"\nvault ready: {bubble_home}")
    print(f"  imported now:    {totals['imported']}")
    print(f"  already vaulted: {totals['skipped']}")
    if totals["missing_record"]:
        print(f"  no RECORD file:  {totals['missing_record']}  (silent skip)")
    if totals["errors"]:
        print(f"  errors:          {totals['errors']}")
    print()
    print("try:")
    print("  bubble vault list                 # see what's vaulted")
    print("  bubble run your-script.py         # vault-only by default")
    print("  bubble run your-script.py --fetch # allow PyPI fallback")
    return 0 if totals["errors"] == 0 else 2


def cmd_probe(args: argparse.Namespace) -> int:
    """bubble probe — interrogate the machine, write host.toml."""
    from . import probe
    config.ensure_dirs()
    results = probe.run_all()
    out_path = probe.host_toml_path()
    probe.write(out_path, results)
    if args.show:
        print(out_path.read_text())
    else:
        # Brief summary
        sub_names = [s["name"] + (f" [{s['status']}]") for s in results["substrates"]]
        print(f"  wrote {out_path}")
        print(f"  kernel:    {results['kernel']['system']} {results['kernel']['release']} {results['kernel']['machine']}")
        print(f"  libc:      {results['libc'].get('variant')} {results['libc'].get('version', '')}")
        print(f"  python:    {results['python']['version']} ({results['python']['executable']})")
        print(f"  shared:    {results['python']['shared']}")
        print(f"  dlmopen:   {results['dlmopen'].get('available')}")
        print(f"  embed:     {results['libpython_embeddable'].get('embeddable')}")
        print(f"  sub-int:   {results['subinterpreters'].get('available')}")
        print(f"  substrates available:")
        for s in results["substrates"]:
            cost = f"~{s['cost_mb']}MB" if s.get("cost_mb") is not None else "n/a"
            print(f"    • {s['name']:<22} cost={cost:<8} {s['status']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """bubble run <script.py> — demand-paged execution.

    No pre-scan, no resolve, no assemble. The meta-path finder traps misses
    and serves them from the vault. Network is opt-in: pass --fetch (or
    set BUBBLE_AUTOFETCH=1) to allow vault misses to escalate to PyPI.
    The strict default keeps every run sovereign — same script, same
    bytes, no silent network.
    """
    db.init_db()
    script = Path(args.script).resolve()
    if not script.exists():
        print(f"error: script not found: {script}", file=sys.stderr)
        return 1

    from .meta_finder import install, _load_scope, _load_aliases
    scope = aliases = None
    if args.scope:
        scope = _load_scope(Path(args.scope))
        aliases = _load_aliases(Path(args.scope))
    autofetch = bool(args.fetch) or bool(os.environ.get("BUBBLE_AUTOFETCH"))
    finder = install(
        scope=scope or None,
        aliases=aliases or None,
        autofetch=autofetch,
        verbose=args.verbose,
    )

    # Execute the script in-process so the finder traps every import
    # Strip system site-packages so the vault is the only source
    if args.isolate:
        sys.path = [p for p in sys.path
                    if "dist-packages" not in p and "site-packages" not in p]

    sys.argv = [str(script), *(args.args or [])]
    code = compile(script.read_text(), str(script), "exec")
    globs = {"__name__": "__main__", "__file__": str(script)}

    try:
        exec(code, globs)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception:
        import traceback
        traceback.print_exc()
        rc = 1
    else:
        rc = 0

    if args.lock:
        _write_lockfile(Path(args.lock), finder.hits)
        if args.verbose:
            print(f"  wrote lockfile: {args.lock} ({len(finder.hits)} entries)",
                  file=sys.stderr)
    return rc


def cmd_bridge(args: argparse.Namespace) -> int:
    """bubble bridge <script> — orchestrate main + legacy runners."""
    return bridge.run(args)


def _write_lockfile(path: Path, hits: list) -> None:
    """A run's hit log IS the lockfile. (import_name, package, version, wheel_tag) per line."""
    seen = set()
    lines = ["# bubble lockfile — recorded from a real run",
             "# columns: import_name  package  version  wheel_tag", ""]
    for import_name, package, version, tag in hits:
        key = (import_name, package, version, tag)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{import_name}\t{package}\t{version}\t{tag}")
    path.write_text("\n".join(lines) + "\n")


def cmd_up(args: argparse.Namespace) -> int:
    """bubble up <script.py> — scan, resolve, fetch, assemble, run."""
    from .scanner import py as scanner_py, resolver as resolver_mod
    from .run import assemble as assemble_mod, runner
    import hashlib
    import uuid
    from datetime import datetime
    db.init_db()
    script = Path(args.script).resolve()
    if not script.exists():
        print(f"error: script not found: {script}", file=sys.stderr)
        return 1

    # Stage 1: scan
    iset = scanner_py.scan(script)
    if args.verbose:
        print(f"scan: {len(iset.top_level_imports)} top-level imports "
              f"({len(iset.stdlib_imports)} stdlib excluded)")

    # Stage 2: resolve against vault
    plan = resolver_mod.resolve(iset)
    if args.verbose:
        print(f"resolve: {len(plan.resolved)} matched in vault, "
              f"{len(plan.missing)} missing")

    # Stage 3: fetch missing — opt-in only. Strict default keeps `bubble up`
    # offline unless the user (or env) explicitly authorizes network.
    autofetch = bool(args.fetch) or bool(os.environ.get("BUBBLE_AUTOFETCH"))
    if plan.missing and autofetch:
        plan = resolver_mod.fetch_missing(plan, allow_prerelease=args.prerelease)
        if args.verbose:
            print(f"fetch: {len(plan.resolved)} now resolved, "
                  f"{len(plan.missing)} still missing")

    # Stage 4: assemble bubble. uuid4 in the digest avoids collisions between
    # concurrent runs of the same script (timestamp resolution is not enough).
    bubble_id = hashlib.sha256(
        f"{script}{datetime.now().isoformat()}{uuid.uuid4()}".encode()
    ).hexdigest()[:12]
    bubble_dir = config.BUBBLES_DIR / bubble_id
    env = assemble_mod.assemble(plan, bubble_dir)
    if args.verbose:
        print(f"bubble {bubble_id} at {bubble_dir}")

    # Stage 5: run
    cmd = [sys.executable, str(script), *(args.args or [])]
    rc = runner.run(env, cmd, verbose=args.verbose)
    if not args.keep:
        import shutil
        shutil.rmtree(bubble_dir, ignore_errors=True)
    return rc


def cmd_shell_activate(args: argparse.Namespace) -> int:
    db.init_db()
    sd = shell_mod.shell_dir(args.name)
    if not sd.exists():
        print(f"shell does not exist: {args.name}", file=sys.stderr)
        return 1
    print(sd / "activate")
    return 0


def cmd_shell_bundle(args: argparse.Namespace) -> int:
    """bubble shell bundle <name> -o <path> — produce a portable artifact.

    The bundle holds the shell + its vault closure + the integrity facts
    (vault_files rows) the source machine recorded. Untar on a target
    machine with `bubble shell unbundle` and the trust chain travels.
    """
    from . import bundle as bundle_mod
    db.init_db()
    out = Path(args.output).resolve()
    try:
        summary = bundle_mod.bundle(args.name, out)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  bundled {args.name} → {out}")
    print(f"    packages: {summary['packages']}")
    print(f"    files:    {summary['files']}")
    print(f"    size:     {summary['bytes']/1024:.1f}KB")
    return 0


def cmd_shell_unbundle(args: argparse.Namespace) -> int:
    """bubble shell unbundle <tar> — extract a bundle into BUBBLE_HOME.

    The target's vault.db is rebuilt from the bundle's recorded rows;
    the target probes itself to write a fresh host.toml; every
    extracted pin is verified against the source's sha256 facts.
    """
    from . import bundle as bundle_mod
    try:
        result = bundle_mod.unbundle(
            Path(args.tar),
            allow_python_mismatch=args.allow_python_mismatch,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  unbundled {result['shell']} → {result['into_home']}")
    print(f"    packages: {result['packages']}")
    print(f"    source python_tag: {result['source_python']}")
    print(f"    target python_tag: {result['target_python']}")
    if result["drift"]:
        print(f"    DRIFT in transit ({len(result['drift'])} packages):")
        for d in result["drift"]:
            print(f"      × {d}")
        print(f"    Recorded as `vault_drift_*` in host.toml. The shell "
              f"is on disk but its drifted entries will refuse to link "
              f"on use. Investigate before activating.")
        return 2
    print(f"    integrity: clean (every extracted file matches source sha256)")
    return 0


# ───────────────────────────── argument tree ─────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bubble",
                                description="content-addressed package vault + thin runtime views")
    sub = p.add_subparsers(dest="command", required=True)

    # vault
    vault = sub.add_parser("vault", help="manage the package vault")
    vsub = vault.add_subparsers(dest="vault_cmd", required=True)

    vsub.add_parser("list", help="list vaulted packages").set_defaults(func=cmd_vault_list)

    iv = vsub.add_parser("import-venv", help="import packages from a venv site-packages")
    iv.add_argument("path", help="path to a venv root or its site-packages dir")
    iv.add_argument("--copy", dest="hardlink", action="store_false",
                    help="copy files into the vault instead of hardlinking "
                         "(default is hardlink, with automatic fallback to "
                         "copy on cross-filesystem)")
    iv.add_argument("--hardlink", dest="hardlink", action="store_true",
                    help=argparse.SUPPRESS)  # kept for compatibility; default
    iv.set_defaults(hardlink=True)
    iv.add_argument("--overwrite", action="store_true",
                    help="re-import even if already vaulted at same (name,version,tag)")
    iv.add_argument("--skip", nargs="*", help="package names to skip")
    iv.add_argument("--verbose", "-v", action="store_true")
    iv.set_defaults(func=cmd_vault_import_venv)

    af = vsub.add_parser("audit-fs", help="scan filesystem for venvs and report duplicates")
    af.add_argument("--root", default="/", help="root to scan (default /)")
    af.set_defaults(func=cmd_vault_audit_fs)

    vr = vsub.add_parser("remove", help="remove a specific (name,version,tag) from vault")
    vr.add_argument("name")
    vr.add_argument("version")
    vr.add_argument("tag")
    vr.set_defaults(func=cmd_vault_remove)

    vg = vsub.add_parser("get", help="download a package from PyPI into the vault")
    vg.add_argument("package")
    vg.add_argument("--version", "-V", help="pin to exact version")
    vg.add_argument("--prerelease", action="store_true",
                    help="allow alpha/beta/rc/dev versions")
    vg.add_argument("--overwrite", action="store_true",
                    help="re-fetch even if already vaulted")
    vg.add_argument("--allow-sdist", action="store_true",
                    help="permit sdist builds (runs setup.py / build "
                         "backend; trust boundary crossed; default off)")
    vg.set_defaults(func=cmd_vault_get)

    # shell
    sh = sub.add_parser("shell", help="manage long-lived bubbles")
    ssub = sh.add_subparsers(dest="shell_cmd", required=True)

    sc = ssub.add_parser("create", help="create a new shell")
    sc.add_argument("name")
    sc.add_argument("specs", nargs="*", help="package specs (pkg or pkg==version)")
    sc.add_argument("--exist-ok", action="store_true")
    sc.add_argument("--from", dest="from_manifest", metavar="MANIFEST",
                    help="create from a deployment manifest "
                         "(see bubble.manifest format)")
    sc.add_argument("--fetch", action="store_true",
                    help="when --from is set, pull any missing pin from PyPI; "
                         "default is vault-only (fail closed on missing pins)")
    sc.set_defaults(func=cmd_shell_create)

    sa = ssub.add_parser("add", help="add packages to a shell")
    sa.add_argument("name")
    sa.add_argument("specs", nargs="+")
    sa.set_defaults(func=cmd_shell_add)

    sr = ssub.add_parser("remove", help="unlink packages from a shell")
    sr.add_argument("name")
    sr.add_argument("pkgs", nargs="+")
    sr.set_defaults(func=cmd_shell_remove)

    ssub.add_parser("list", help="list shells").set_defaults(func=cmd_shell_list)

    sd = ssub.add_parser("delete", help="delete a shell")
    sd.add_argument("name")
    sd.set_defaults(func=cmd_shell_delete)

    se = ssub.add_parser("exec", help="exec a command with shell PYTHONPATH/PATH set")
    se.add_argument("name")
    se.add_argument("cmd", nargs=argparse.REMAINDER)
    se.set_defaults(func=cmd_shell_exec)

    sact = ssub.add_parser("activate", help="print path to activate script")
    sact.add_argument("name")
    sact.set_defaults(func=cmd_shell_activate)

    sb = ssub.add_parser("bundle", help="bundle a shell + its vault closure into a tar.gz")
    sb.add_argument("name")
    sb.add_argument("-o", "--output", required=True,
                    help="output path for the tar.gz bundle")
    sb.set_defaults(func=cmd_shell_bundle)

    sub_un = ssub.add_parser("unbundle",
                             help="extract a bundle into BUBBLE_HOME, "
                                  "rebuild vault.db, probe, verify")
    sub_un.add_argument("tar", help="path to a bubble bundle (.tar.gz)")
    sub_un.add_argument("--allow-python-mismatch", action="store_true",
                        help="extract even when source/target python_tag differ "
                             "(verify will still refuse drifted pins)")
    sub_un.set_defaults(func=cmd_shell_unbundle)

    # up — ephemeral per-script bubble (the original entry point)
    up = sub.add_parser("up", help="scan a script, assemble an ephemeral bubble, run")
    up.add_argument("script")
    up.add_argument("args", nargs="*")
    up.add_argument("--keep", action="store_true", help="don't dissolve bubble after run")
    up.add_argument("--fetch", action="store_true",
                    help="opt in to PyPI fetches for missing packages "
                         "(default: vault-only, fail closed)")
    up.add_argument("--prerelease", action="store_true")
    up.add_argument("--verbose", "-v", action="store_true")
    up.set_defaults(func=cmd_up)

    # setup — zero-flag bootstrap (probe + scan all site-packages)
    st = sub.add_parser("setup",
        help="bootstrap: probe the host and import every site-packages into the vault")
    st.set_defaults(func=cmd_setup)

    # probe — write host.toml self-portrait
    pr = sub.add_parser("probe",
        help="interrogate the machine; write ~/.bubble/host.toml")
    pr.add_argument("--show", action="store_true",
                    help="print the full toml after writing")
    pr.set_defaults(func=cmd_probe)

    # host — show what bubble currently knows (consult side of the loop)
    h = sub.add_parser("host",
        help="show what bubble knows about this machine + recorded failures")
    h.set_defaults(func=cmd_host)

    # run — demand-paged execution, no materialized bubble
    run = sub.add_parser("run",
        help="run a script with imports faulted in from the vault on demand")
    run.add_argument("script")
    run.add_argument("args", nargs="*")
    run.add_argument("--fetch", action="store_true",
                     help="opt in to PyPI fetches on vault miss "
                          "(default: vault-only, fail closed)")
    run.add_argument("--scope", help="path to a scope manifest (TOML)")
    run.add_argument("--lock", help="record actually-loaded closure to this lockfile")
    run.add_argument("--isolate", action="store_true",
                     help="strip system site-packages from sys.path; vault is sole source")
    run.add_argument("--verbose", "-v", action="store_true")
    run.set_defaults(func=cmd_run)

    # bridge — route .py to main bubble and .js/.ts to legacy with guardrails
    br = sub.add_parser("bridge",
        help="route .py to main bubble and .js/.ts to legacy bubble with guardrails")
    br.add_argument("script")
    br.add_argument("args", nargs="*")
    br.add_argument("--fetch", action="store_true",
                    help="for .py routes only: authorize PyPI fetch on vault miss")
    br.add_argument("--no-isolate", action="store_true",
                    help="for .py routes only: keep system site-packages on sys.path")
    br.add_argument("--allow-legacy-network", action="store_true",
                    help="for .js/.ts routes only: authorize legacy network fetch")
    br.add_argument("--keep", action="store_true",
                    help="for legacy routes only: keep ephemeral bubble after run")
    br.add_argument("--dry-run", action="store_true",
                    help="print selected command and exit")
    br.set_defaults(func=cmd_bridge)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


# ─────────────────────────── PyPI fetch ──────────────────────────────────


def cmd_vault_get(args: argparse.Namespace) -> int:
    """bubble vault get <package> [--version V] — download from PyPI into vault."""
    from .vault import fetcher
    db.init_db()
    pin = args.version
    try:
        result = fetcher.fetch_into_vault(
            args.package,
            pinned_version=pin,
            allow_prerelease=args.prerelease,
            overwrite=args.overwrite,
            allow_sdist=args.allow_sdist,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as e:
        # Sovereignty refusals (sdist, off-host index, name mismatch) and
        # other fetch-time guardrails. Surface clean to the caller.
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not result:
        print(f"already vaulted, or no compatible release for {args.package}"
              + (f"=={pin}" if pin else ""))
        return 0
    name, version, tag = result
    print(f"  ✓ vaulted {name}=={version} ({tag})")
    return 0
