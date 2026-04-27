"""Demand-paged Python imports backed by the bubble vault.

A MetaPathFinder that intercepts top-level import misses and resolves them
out of the vault's content store. Stdlib short-circuits before we run.

Plus alias support — multi-version coexistence in one process. The script
writes `import click_old` and `import click_new`; the finder serves each from
a different vault dir under a different sys.modules key.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional


_STDLIB = getattr(sys, "stdlib_module_names", frozenset())
_MYPYC_RE = re.compile(r"^[0-9a-f]+__mypyc$")
_NAMESPACE_ROOTS = frozenset({"backports", "google", "zope", "ruamel"})


def _untrappable(name: str) -> bool:
    if not name:
        return True
    if name.startswith("_"):
        return True
    # Note: mypyc-mangled names ARE trappable now — they live in the
    # vault next to the package that imports them, and the finder
    # resolves them via _lookup_mypyc_helper. Skipping them here would
    # break --isolate runs of any package whose deps were mypyc-built
    # (charset-normalizer, chardet, increasingly common).
    if name in _NAMESPACE_ROOTS:
        return True
    return False


class VaultFinder(importlib.abc.MetaPathFinder):
    """Translate a top-level import → vault dir → standard PathFinder.

    Once we hand a vault path to PathFinder, Python's normal machinery handles
    everything. We just answer 'where is the top-level package?'.
    """

    def __init__(
        self,
        *,
        scope: Optional[dict[str, tuple[str, str]]] = None,
        aliases: Optional[dict] = None,
        autofetch: bool = False,
        verbose: bool = False,
    ) -> None:
        # scope: {pkg_name: (version, wheel_tag)} — pin versions per package.
        # aliases: legacy 3-tuple `(real_name, version, wheel_tag)` or
        #   4-tuple `(real_name, version, wheel_tag, substrate)` where
        #   substrate is None or one of route.SUBSTRATE_LADDER.
        # We normalize everything to 4-tuples internally so downstream
        # code doesn't branch on shape.
        self._scope = scope
        self._aliases = self._normalize_aliases(aliases or {})
        self._autofetch = autofetch
        self._verbose = verbose
        self._hit_log: list[tuple[str, str, str, str]] = []
        self._fetch_failed: set[str] = set()
        # Per-process integrity cache. The first lookup of a vault entry
        # within a run incurs a stat-storm; subsequent lookups inside the
        # same process trust the cache. A re-run gets a fresh cache.
        self._verified: dict[tuple[str, str, str], bool] = {}
        # Per-process routing cache — alias name → resolved substrate.
        # Once an alias has been routed in this process we don't re-consult
        # host.toml on every submodule import.
        self._routed: dict[str, str] = {}

    @staticmethod
    def _normalize_aliases(aliases: dict) -> dict[str, tuple[str, str, str, Optional[str]]]:
        """Accept legacy 3-tuple aliases and new 4-tuple ones; also
        accept manifest.AliasPin objects."""
        out: dict[str, tuple[str, str, str, Optional[str]]] = {}
        for alias, val in aliases.items():
            if hasattr(val, "name") and hasattr(val, "substrate"):
                out[alias] = (val.name, val.version, val.wheel_tag,
                              val.substrate)
            elif isinstance(val, tuple):
                if len(val) == 3:
                    out[alias] = (val[0], val[1], val[2], None)
                elif len(val) == 4:
                    out[alias] = val
                else:
                    raise ValueError(
                        f"alias {alias!r}: tuple must be 3 or 4 elements")
            else:
                raise ValueError(
                    f"alias {alias!r}: unsupported value type {type(val).__name__}")
        return out

    @property
    def hits(self) -> list[tuple[str, str, str, str]]:
        return list(self._hit_log)

    def find_spec(self, fullname, path, target=None):
        top = fullname.split(".", 1)[0]

        # Submodule of an alias: e.g. `click_old.testing`.
        if "." in fullname and top in self._aliases:
            real_name, version, wheel_tag, _substrate = self._aliases[top]
            vault_path = self._alias_vault_path(real_name, version, wheel_tag)
            if vault_path is None:
                return None
            pkg_root = vault_path / real_name
            spec = importlib.machinery.PathFinder.find_spec(
                fullname, [str(pkg_root)], target,
            )
            if spec is not None and spec.loader is not None:
                spec.loader = _SubAliasLoader(spec.loader, top, real_name)
            return spec

        if "." in fullname:
            return None

        if top in _STDLIB:
            return None
        if top in sys.builtin_module_names:
            return None

        if top in self._aliases:
            return self._spec_for_alias(top)

        if _untrappable(top):
            return None
        if top in self._fetch_failed:
            return None

        # mypyc-compiled packages ship a hex-prefixed helper module
        # (e.g. `81d243bd2c585b0f4821__mypyc`) at the top level alongside
        # the package they support. Under --isolate we have no system
        # site-packages to fall back on, so search the vault directly
        # for a matching `.so` and serve it.
        if _MYPYC_RE.match(top):
            spec = self._spec_for_mypyc_helper(top, target)
            if spec is not None:
                return spec
            return None

        vault_path = self._lookup(top)
        if vault_path is None:
            if self._autofetch:
                vault_path = self._fault_to_pypi(top)
            if vault_path is None:
                return None

        spec = importlib.machinery.PathFinder.find_spec(top, [str(vault_path)], target)
        if spec is not None and self._verbose:
            sys.stderr.write(f"[bubble] {fullname} → {vault_path}\n")
        return spec

    # ───────────────────── alias path ─────────────────────

    def _alias_vault_path(self, real_name, version, wheel_tag) -> Optional[Path]:
        from . import config
        if not config.VAULT_DB.exists():
            return None
        try:
            conn = sqlite3.connect(str(config.VAULT_DB))
        except sqlite3.Error:
            return None
        try:
            row = conn.execute(
                "SELECT vault_path FROM packages WHERE name=? AND version=? AND wheel_tag=?",
                (real_name, version, wheel_tag),
            ).fetchone()
        finally:
            conn.close()
        return Path(row[0]) if row else None

    # ───────────────── importlib.metadata per-alias ─────────────────
    #
    # Modern packages compute their own version (and increasingly their
    # entry-point graph) via importlib.metadata.version(__name__) instead
    # of a hardcoded string — click 8.3+, for example. Without this hook,
    # an aliased click_v0 calling version("click") would walk sys.path
    # and report whatever click is installed in the host venv, silently
    # collapsing the diamond-conflict story for any metadata-driven tool.
    #
    # The contract:
    #   - When called from inside an alias namespace, return the vault
    #     dist-info for that alias's pinned version.
    #   - When called from outside any alias, don't claim — let the host's
    #     standard finders resolve normally.
    #
    # The alias scope is determined by walking the call stack, since
    # importlib.metadata's API has no channel for "who's asking."
    def find_distributions(self, context=None):
        import importlib.metadata as md

        scope_alias = self._caller_alias_scope()
        if scope_alias is None or scope_alias not in self._aliases:
            return

        real_name, version, wheel_tag, _sub = self._aliases[scope_alias]

        requested_name = getattr(context, "name", None) if context is not None else None
        if requested_name is not None:
            from .vault.metadata import normalize_name
            if normalize_name(requested_name) != normalize_name(real_name):
                return

        vault_path = self._alias_vault_path(real_name, version, wheel_tag)
        if vault_path is None:
            return

        # Distinfo dirs follow `<name>-<version>.dist-info` per PEP 427.
        # Try the literal name first, then the PEP 503 underscore form.
        candidates = list(vault_path.glob(f"{real_name}-{version}.dist-info"))
        if not candidates:
            from .vault.metadata import normalize_name
            norm = normalize_name(real_name).replace("-", "_")
            candidates = list(vault_path.glob(f"{norm}-{version}.dist-info"))
        if not candidates:
            candidates = list(vault_path.glob("*.dist-info"))
        if not candidates:
            return

        if self._verbose:
            sys.stderr.write(
                f"[bubble] metadata for {requested_name or '*'} → "
                f"{scope_alias} ({real_name}=={version}) "
                f"from {candidates[0].name}\n"
            )
        yield md.PathDistribution(candidates[0])

    def _caller_alias_scope(self) -> Optional[str]:
        """Walk the calling frames to find the nearest alias namespace.

        Returns the alias name (e.g. 'click_v0') if any frame on the stack
        belongs to an alias module or its submodules; None otherwise. We
        skip our own frame and any importlib internals — alias code is
        always somewhere further up."""
        f = sys._getframe(1)
        while f is not None:
            mod_name = f.f_globals.get("__name__", "")
            if mod_name in self._aliases:
                return mod_name
            if "." in mod_name:
                top = mod_name.split(".", 1)[0]
                if top in self._aliases:
                    return top
            f = f.f_back
        return None

    def _spec_for_alias(self, alias: str):
        real_name, version, wheel_tag, requested_substrate = self._aliases[alias]

        # Substrate routing: consult host portrait + recorded history.
        if alias not in self._routed:
            from . import route
            decision = route.route(alias, requested_substrate)
            if decision.actual is None:
                route.record_decision(decision)
                if self._verbose:
                    sys.stderr.write(
                        f"[bubble] alias {alias} unavailable: {decision.reason}\n"
                    )
                return None
            if decision.downgraded_from and not decision.learned_from_history:
                route.record_decision(decision)
            self._routed[alias] = decision.actual
            if self._verbose and decision.downgraded_from:
                tag = "[learned]" if decision.learned_from_history else "[fresh]"
                sys.stderr.write(
                    f"[bubble] alias {alias} {tag} "
                    f"{decision.downgraded_from} → {decision.actual}: "
                    f"{decision.reason}\n"
                )

        actual_substrate = self._routed[alias]

        # Route to the dlmopen-isolated substrate handler. The handler
        # builds an IsolatedModule (a types.ModuleType subclass) that
        # the calling interpreter can use as if it were the real module.
        if actual_substrate == "dlmopen_isolated":
            return self._spec_via_dlmopen(alias, real_name, version, wheel_tag)

        # Route to the subprocess-isolated substrate handler. Same
        # IsolatedModule shape as dlmopen, but the isolation boundary
        # is an OS process rather than a link namespace — portable
        # everywhere Python runs, ~30MB per alias, full thread/signal
        # isolation by construction.
        if actual_substrate == "subprocess":
            return self._spec_via_subprocess(alias, real_name, version, wheel_tag)

        # Default: in_process path (the existing direct-link route).
        vault_path = self._alias_vault_path(real_name, version, wheel_tag)
        if vault_path is None:
            return None

        # Two layouts: a package directory (`<real>/__init__.py`) or a flat
        # single-file module (`<real>.py`, e.g. `six`). Try the package
        # form first — it's the common case and gives us submodule_search.
        pkg_dir = vault_path / real_name
        init = pkg_dir / "__init__.py"
        if init.exists():
            target = init
            search = [str(pkg_dir)]
        else:
            flat = vault_path / f"{real_name}.py"
            if not flat.is_file():
                return None
            target = flat
            search = None  # flat module: not a package, no submodule path

        if self._verbose:
            sys.stderr.write(
                f"[bubble] alias {alias} → {real_name}=={version} [{wheel_tag}]\n"
            )
        inner = importlib.machinery.SourceFileLoader(alias, str(target))
        spec = importlib.util.spec_from_file_location(
            alias, str(target),
            loader=_AliasLoader(inner, alias, real_name),
            submodule_search_locations=search,
        )
        return spec

    def _spec_via_dlmopen(self, alias: str, real_name: str,
                          version: str, wheel_tag: str):
        """Build an import spec backed by the dlmopen-isolated substrate.

        The loader returns an IsolatedModule whose attribute access
        marshals into a fresh dlmopen-isolated libpython. The vault
        path is verified through the same C1 integrity edge as the
        in_process route — drift refuses the alias before any isolated
        interpreter is spun up."""
        if not self._verify_or_record(real_name, version, wheel_tag):
            return None
        vault_path = self._alias_vault_path(real_name, version, wheel_tag)
        if vault_path is None:
            return None

        from .substrate import dlmopen as _dlmopen
        loader = _DlmopenAliasLoader(alias, vault_path, real_name, _dlmopen)
        spec = importlib.util.spec_from_loader(alias, loader)
        if self._verbose:
            sys.stderr.write(
                f"[bubble] alias {alias} → dlmopen-isolated: "
                f"{real_name}=={version} [{wheel_tag}]\n"
            )
        return spec

    def _spec_via_subprocess(self, alias: str, real_name: str,
                             version: str, wheel_tag: str):
        """Build an import spec backed by the subprocess-isolated substrate.

        Mirrors _spec_via_dlmopen — same integrity gate, same proxy
        module shape — but the isolation boundary is an OS process.
        The loader returns an IsolatedModule whose attribute access
        marshals into a child Python over a length-prefixed pickle
        channel. The vault path is verified before any child is
        spawned, so vault drift refuses the alias before any cost is
        paid."""
        if not self._verify_or_record(real_name, version, wheel_tag):
            return None
        vault_path = self._alias_vault_path(real_name, version, wheel_tag)
        if vault_path is None:
            return None

        from .substrate import subprocess as _subprocess_sub
        loader = _SubprocessAliasLoader(
            alias, vault_path, real_name, _subprocess_sub,
        )
        spec = importlib.util.spec_from_loader(alias, loader)
        if self._verbose:
            sys.stderr.write(
                f"[bubble] alias {alias} → subprocess-isolated: "
                f"{real_name}=={version} [{wheel_tag}]\n"
            )
        return spec

    def _spec_for_mypyc_helper(self, name: str, target):
        """Find a mypyc helper `.so` anywhere in the vault and serve it.

        The helper's name has a unique hex prefix per build, so a
        vault-wide glob hits at most one file. We import it as a
        top-level module via PathFinder against its containing
        directory; ExtensionFileLoader handles the actual load."""
        from . import config
        if not config.VAULT_DIR.exists():
            return None
        for candidate in config.VAULT_DIR.rglob(f"{name}.*.so"):
            if not candidate.is_file():
                continue
            container = candidate.parent
            spec = importlib.machinery.PathFinder.find_spec(
                name, [str(container)], target,
            )
            if spec is not None:
                if self._verbose:
                    sys.stderr.write(
                        f"[bubble] mypyc helper {name} → {candidate}\n"
                    )
                return spec
        return None

    # ───────────────────── default path ─────────────────────

    def _lookup(self, name: str) -> Optional[Path]:
        from . import config
        from .vault import metadata as meta
        if not config.VAULT_DB.exists():
            return None
        try:
            conn = sqlite3.connect(str(config.VAULT_DB))
        except sqlite3.Error:
            return None
        try:
            row = self._query_vault(conn, name, meta.normalize_name(name))
        finally:
            conn.close()
        if row is None:
            return None
        pkg_name, version, wheel_tag, vault_path = row
        if not self._verify_or_record(pkg_name, version, wheel_tag):
            # Drift was recorded against host.toml; refuse the link rather
            # than serve bytes the vault no longer vouches for.
            return None
        self._hit_log.append((name, pkg_name, version, wheel_tag))
        return Path(vault_path)

    def _verify_or_record(self, pkg_name: str, version: str, wheel_tag: str) -> bool:
        """Verify a vault entry once per process. On drift, record to
        host.toml and return False. The default is on; BUBBLE_VERIFY=0
        opts out (the explicit trust-the-disk mode for performance work
        or when the operator has already verified externally)."""
        if os.environ.get("BUBBLE_VERIFY") == "0":
            return True
        key = (pkg_name, version, wheel_tag)
        cached = self._verified.get(key)
        if cached is not None:
            return cached
        from .vault import store
        from . import host
        report = store.verify(pkg_name, version, wheel_tag)
        if not report.had_index:
            # Pre-v3 vault entry — no integrity facts on file. Pass through;
            # the operator can `bubble vault rehash` to populate.
            self._verified[key] = True
            return True
        if report.clean:
            self._verified[key] = True
            return True
        target = f"{pkg_name}=={version}@{wheel_tag}"
        for rel, kind in report.drifted:
            host.record_failure(kind, target, f"rel={rel}")
        for rel in report.missing:
            host.record_failure("vault_drift_missing", target, f"rel={rel}")
        if report.extra and self._verbose:
            sys.stderr.write(
                f"[bubble] note: {target} has {len(report.extra)} extra files "
                f"on disk not listed in vault_files; not refusing the lookup "
                f"(ignored unless --strict)\n"
            )
        if report.drifted or report.missing:
            sys.stderr.write(
                f"[bubble] vault drift refusing {target}: "
                f"{len(report.drifted)} modified, {len(report.missing)} missing. "
                f"Run `bubble vault rehash {pkg_name} {version} {wheel_tag}` "
                f"to re-record, or `bubble vault remove ...` to drop the entry.\n"
            )
            self._verified[key] = False
            return False
        self._verified[key] = True
        return True

    def _query_vault(self, conn, import_name, normalized):
        rows = list(conn.execute(
            "SELECT p.name, p.version, p.wheel_tag, p.vault_path "
            "FROM packages p JOIN top_level t "
            "  ON p.name=t.package AND p.version=t.version AND p.wheel_tag=t.wheel_tag "
            "WHERE t.import_name = ?",
            (import_name,),
        ))
        if not rows:
            rows = list(conn.execute(
                "SELECT name, version, wheel_tag, vault_path FROM packages "
                "WHERE name=? OR LOWER(REPLACE(REPLACE(name,'_','-'),'.','-')) = ?",
                (import_name, normalized),
            ))
        if not rows:
            return None
        if self._scope:
            # Scope is a *pin* — for packages listed here, only allow the
            # specified (version, tag). For packages NOT listed, fall through
            # to default best-pick. This lets users pin what they care about
            # without enumerating every transitive dep.
            from .vault import metadata as _meta
            scope_norm = {_meta.normalize_name(k): v for k, v in self._scope.items()}
            pkg_norm_set = {_meta.normalize_name(r[0]) for r in rows}
            scoped_pkgs = pkg_norm_set & set(scope_norm)
            if scoped_pkgs:
                # At least one matching package is pinned — filter to the pin
                allowed = []
                for name, version, tag, path in rows:
                    spec = scope_norm.get(_meta.normalize_name(name))
                    if spec and (version, tag) == spec:
                        allowed.append((name, version, tag, path))
                if not allowed:
                    return None
                rows = allowed
            # else: no pin for this package; fall through to free pick
        from .run.shell import _version_key, _wheel_tag_score
        rows.sort(key=lambda r: (_version_key(r[1]), _wheel_tag_score(r[2])),
                  reverse=True)
        return rows[0]

    def _fault_to_pypi(self, name: str) -> Optional[Path]:
        from .scanner.py import IMPORT_TO_DIST
        from . import host
        dist = IMPORT_TO_DIST.get(name, name)

        # Cross-run memory. _fetch_failed holds the in-process view; host.toml
        # holds the persistent view. Without this read the function records
        # but never consults — every new process re-asks PyPI for a dist
        # that already failed and writes another duplicate row.
        if (host.is_known_failure("pypi_fetch_failed", dist) or
                host.is_known_failure("pypi_no_compatible_release", dist)):
            self._fetch_failed.add(name)
            if self._verbose:
                sys.stderr.write(
                    f"[bubble] skip fetch for {name!r}: known failure on this host\n"
                )
            return None

        if self._verbose:
            extra = f" (dist={dist})" if dist != name else ""
            sys.stderr.write(f"[bubble] vault miss for {name!r}, fetching from PyPI{extra}…\n")
        try:
            from .vault import fetcher
            result = fetcher.fetch_into_vault(dist)
        except (ValueError, RuntimeError) as exc:
            # Sovereignty refusals from the fetcher (sdist gate, off-host
            # index, name swap, https-required). These are policy fails,
            # not network fails — give them their own kind.
            if self._verbose:
                sys.stderr.write(f"[bubble] fetch refused: {exc}\n")
            self._fetch_failed.add(name)
            host.record_failure("pypi_index_refused", dist,
                                f"{type(exc).__name__}: {exc}")
            return None
        except Exception as exc:
            if self._verbose:
                sys.stderr.write(f"[bubble] fetch failed: {exc}\n")
            self._fetch_failed.add(name)
            host.record_failure("pypi_fetch_failed", dist,
                                f"{type(exc).__name__}: {exc}")
            return None
        if not result:
            self._fetch_failed.add(name)
            host.record_failure("pypi_no_compatible_release", dist,
                                f"import_name={name}")
            return None
        return self._lookup(name)


class _DlmopenAliasLoader(importlib.abc.Loader):
    """Loader that hands back an IsolatedModule for the dlmopen-isolated
    substrate. Python's import machinery calls create_module to get the
    module object; we return the proxy. exec_module is a no-op because
    the proxy is already fully formed — no source to execute in the
    caller's interpreter."""

    def __init__(self, alias: str, vault_path, real_name: str, dlmopen_mod):
        self._alias = alias
        self._vault_path = vault_path
        self._real = real_name
        self._dlmopen = dlmopen_mod

    def create_module(self, spec):
        return self._dlmopen.load_module(self._alias, self._vault_path, self._real)

    def exec_module(self, module):
        return None


class _SubprocessAliasLoader(importlib.abc.Loader):
    """Loader that hands back an IsolatedModule for the subprocess-isolated
    substrate. Same shape as _DlmopenAliasLoader; the substrate handler
    underneath is what differs — child OS process instead of dlmopen
    link namespace."""

    def __init__(self, alias: str, vault_path, real_name: str, subprocess_mod):
        self._alias = alias
        self._vault_path = vault_path
        self._real = real_name
        self._sub = subprocess_mod

    def create_module(self, spec):
        return self._sub.load_module(self._alias, self._vault_path, self._real)

    def exec_module(self, module):
        return None


class _AliasLoader(importlib.abc.Loader):
    """Load a package as `<alias>` while its internal `from <real_name> import x`
    statements continue to work — by temporarily binding `<real_name>` to the
    alias module in sys.modules during exec.
    """

    def __init__(self, inner, alias: str, real_name: str):
        self._inner = inner
        self._alias = alias
        self._real = real_name

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        saved = sys.modules.pop(self._real, None)
        saved_subs = {k: sys.modules.pop(k) for k in list(sys.modules)
                      if k.startswith(f"{self._real}.")}
        sys.modules[self._real] = module
        try:
            self._inner.exec_module(module)
            # Re-key newly-loaded <real>.* into <alias>.*
            for k in list(sys.modules):
                if k == self._real or k.startswith(f"{self._real}."):
                    aliased = k.replace(self._real, self._alias, 1)
                    if aliased not in sys.modules:
                        sys.modules[aliased] = sys.modules[k]
        finally:
            for k in list(sys.modules):
                if k == self._real or k.startswith(f"{self._real}."):
                    if k != self._real or saved is None:
                        del sys.modules[k]
            if saved is not None:
                sys.modules[self._real] = saved
            for k, v in saved_subs.items():
                sys.modules[k] = v


class _SubAliasLoader(importlib.abc.Loader):
    """For submodules of an alias: e.g. loading `click_old.testing`.

    The submodule's source has internal `from click import x`, which must
    resolve to `click_old`, not whatever default `click` is in sys.modules.
    Same swap-and-restore trick as _AliasLoader, scoped to a submodule.
    """

    def __init__(self, inner, alias_top: str, real_name: str):
        self._inner = inner
        self._alias_top = alias_top
        self._real = real_name

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # Temporarily expose alias_top as real_name so internal absolute
        # imports of the real package name resolve to the aliased copy.
        saved = sys.modules.get(self._real)
        if self._alias_top in sys.modules:
            sys.modules[self._real] = sys.modules[self._alias_top]
        try:
            self._inner.exec_module(module)
        finally:
            if saved is not None:
                sys.modules[self._real] = saved
            elif self._real in sys.modules:
                del sys.modules[self._real]


def install(
    *,
    scope: Optional[dict[str, tuple[str, str]]] = None,
    aliases: Optional[dict] = None,
    autofetch: bool = False,
    verbose: bool = False,
) -> VaultFinder:
    """Register the finder on sys.meta_path. Aliases need front-of-list.

    `aliases` accepts the legacy 3-tuple form `(real_name, version,
    wheel_tag)` and the new 4-tuple `(real_name, version, wheel_tag,
    substrate)`. AliasPin objects from `bubble.manifest` are also
    accepted. The 4th element / substrate field declares which
    isolation substrate the alias should be hosted on; the router
    consults host.toml at resolution time.
    """
    finder = VaultFinder(scope=scope, aliases=aliases, autofetch=autofetch, verbose=verbose)
    if aliases:
        sys.meta_path.insert(0, finder)
    else:
        sys.meta_path.append(finder)
    return finder


def install_from_env() -> Optional[VaultFinder]:
    if not os.environ.get("BUBBLE_AUTOFAULT"):
        return None
    scope = aliases = None
    scope_path = os.environ.get("BUBBLE_SCOPE")
    if scope_path:
        scope = _load_scope(Path(scope_path))
        aliases = _load_aliases(Path(scope_path))
    return install(
        scope=scope or None,
        aliases=aliases or None,
        autofetch=bool(os.environ.get("BUBBLE_AUTOFETCH")),
        verbose=bool(os.environ.get("BUBBLE_VERBOSE")),
    )


# ─────────────────────── manifest parsing ───────────────────────


def _load_scope(path: Path) -> dict[str, tuple[str, str]]:
    return _load_section(path, "packages",
        r'"([^"]+)"\s*=\s*\{\s*version\s*=\s*"([^"]+)"\s*,'
        r'\s*wheel_tag\s*=\s*"([^"]+)"\s*\}',
        lambda g: (g[0], (g[1], g[2])),
    )


def _load_aliases(path: Path) -> dict[str, tuple[str, str, str]]:
    return _load_section(path, "aliases",
        r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{\s*name\s*=\s*"([^"]+)"\s*,'
        r'\s*version\s*=\s*"([^"]+)"\s*,'
        r'\s*wheel_tag\s*=\s*"([^"]+)"\s*\}',
        lambda g: (g[0], (g[1], g[2], g[3])),
    )


def _load_section(path: Path, section: str, line_re: str, extract):
    out: dict = {}
    if not path.exists():
        return out
    in_section = False
    target = f"[{section}]"
    pattern = re.compile(line_re)
    for line in path.read_text().splitlines():
        line = line.strip()
        if line == target:
            in_section = True
            continue
        if line.startswith("[") and line != target:
            in_section = False
            continue
        if not in_section or not line or line.startswith("#"):
            continue
        m = pattern.match(line)
        if m:
            k, v = extract(m.groups())
            out[k] = v
    return out
