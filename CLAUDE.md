# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Bubble** is a content-addressed package vault plus a meta-path finder that serves Python imports out of the vault on demand. Pure Python, stdlib only, no external dependencies. The README is the canonical description of what bubble does today.

## Architecture

The live codebase is the `bubble/` package. The original monolith is preserved in `legacy/` (see `legacy/README.md`); nothing in `bubble/` imports from it.

### `bubble/` — the live package

```
bubble/
├── cli.py              # entry: vault | shell | run | up | probe | host
│                       #   shell create --from manifest.toml
│                       #   shell bundle / unbundle  (the deployment artifact)
├── meta_finder.py      # VaultFinder on sys.meta_path; alias loaders;
│                       #   verify-on-read + per-process integrity cache;
│                       #   substrate routing dispatch via _spec_via_dlmopen
├── manifest.py         # deployment manifest — Manifest, AliasPin (substrate
│                       #   field reserved + read), load() / dump()
├── bundle.py           # portable artifact codec — tar.gz with bundle
│                       #   manifest (deploy spec + integrity facts) + vault
│                       #   subset + shell tree + source host.toml
├── route.py            # substrate routing — Decision dataclass; ladder
│                       #   subprocess > dlmopen_isolated > sub_interpreter >
│                       #   in_process; consult host.toml history first;
│                       #   record_decision() writes substrate_downgraded
├── substrate/
│   ├── __init__.py     # handler registry; is_implemented(); status()
│   └── dlmopen.py      # DlmopenInterp (Py_Initialize in fresh dlmopen
│                       #   namespace); install_module / get_attr / call_attr
│                       #   (pickle-marshalled call channel); IsolatedModule
│                       #   (types.ModuleType subclass); per-alias interpreter
│                       #   registry with atexit cleanup
├── config.py           # paths, runner-tag detection, ensure_dirs (0o700)
├── probe.py            # write ~/.bubble/host.toml self-portrait + substrates
├── host.py             # read host.toml; FAILURE_KINDS vocabulary;
│                       #   record_failure / record_observation
├── vault/
│   ├── db.py           # schema v3 (adds vault_files for per-file integrity)
│   ├── store.py        # atomic stage→rename; commit-time hash walk
│   │                   #   populates vault_files; verify() returns drift
│   │                   #   report; _index_package_internals fills modules,
│   │                   #   module_imports, dependencies tables
│   ├── fetcher.py      # JSON Simple-API client (urllib + zipfile);
│   │                   #   _validate_index_url enforces https; canonical-
│   │                   #   name validation; sdists refused by default
│   ├── metadata.py     # METADATA/WHEEL parsers, PEP 503 normalization
│   └── importer.py     # `bubble vault import-venv` (refuses symlinks)
├── scanner/
│   ├── py.py           # AST-based Python import scanner
│   └── resolver.py     # match an ImportSet against the vault; fetch missing
└── run/
    ├── assemble.py     # ephemeral bubble assembly (`bubble up`, retired)
    ├── runner.py       # error-loop fallback; records to host.toml
    └── shell.py        # long-lived named shells; relative symlinks;
                        #   verify-on-link; add_pinned for manifest-driven
                        #   creation; metadata blob holds alias substrate
                        #   declarations
```

### Vault layout on disk

```
~/.bubble/
├── vault/<name>/<version>/<wheel_tag>/<unpacked>
├── shells/<name>/{lib,bin,activate,manifest.toml}
├── bubbles/<id>/                # ephemeral, dissolved after `up`
├── wheels/                      # transient downloads
├── vault.db                     # SQLite, schema v2
└── host.toml                    # probe portrait + recorded failures
```

### Database schema (v3)

```sql
packages         -- PK (name, version, wheel_tag); sha256, source, vault_path, has_native
top_level        -- import_name → (package, version, wheel_tag); + import_sha256 over the subtree
vault_files      -- per-file integrity: sha256 + size + mtime_ns per (pkg, ver, tag, rel_path)
dependencies     -- per-package deps                       (FK → packages)
modules          -- per-package module index               (FK → packages)
module_imports   -- per-module import lists                (FK → packages)
shells           -- long-lived named bubbles; metadata JSON holds alias substrate decls
bubbles          -- ephemeral bubbles (legacy path)
schema_meta      -- version sentinel (3)
```

Two integrity edges:

- `top_level.import_sha256` — the cryptographic edge between an *import name* and the bytes the vault serves under it. Computed once at vault-add over a deterministic walk of the asserted subtree. `top_level.txt` is verified against the staged tree at add-time — a name asserted but absent is dropped. Cross-distribution collisions (two distros claiming `cv2`) are recorded in `bubble.vault.store.top_level_contentions` for audit.

- `vault_files.sha256` — the cryptographic edge between *vaulted bytes* and *bytes still on disk*. Populated by the same commit-time tree walk that detects native artifacts. `store.verify(name, version, wheel_tag)` re-checks on read with a stat fast-path (rehash only on size/mtime mismatch). Drift refuses the lookup or link and writes a `vault_drift_*` entry to `host.toml`.

## Commands

```bash
# vault
python3 -m bubble vault list
python3 -m bubble vault get <package> [--version V] [--prerelease] [--overwrite]
python3 -m bubble vault import-venv <site-packages> [--hardlink] [--overwrite]
python3 -m bubble vault audit-fs [--root /]
python3 -m bubble vault remove <name> <version> <tag>

# long-lived shells
python3 -m bubble shell create <name> [pkg ...]
python3 -m bubble shell create <name> --from <manifest.toml> [--fetch]
python3 -m bubble shell add <name> <pkg ...>
python3 -m bubble shell remove <name> <pkg ...>
python3 -m bubble shell list
python3 -m bubble shell delete <name>
python3 -m bubble shell exec <name> -- <cmd ...>
python3 -m bubble shell activate <name>          # prints sourceable path

# deployment artifact
python3 -m bubble shell bundle <name> -o <path.tar.gz>
python3 -m bubble shell unbundle <path.tar.gz> [--allow-python-mismatch]

# run a script
python3 -m bubble run <script.py> [--isolate] [--scope versions.toml] [--lock out.lock] [args...]
python3 -m bubble up  <script.py> [--keep] [args...]    # ephemeral; retired

# self-portrait
python3 -m bubble probe [--show]
python3 -m bubble host
```

The README's installation path is `bubble.pyz` — a stdlib-only zipapp built from the package.

## Tests

```bash
python3 tests/run.py                 # all
python3 tests/run.py 10_breakers     # filter by tier
python3 tests/run.py --no-md         # skip RESULTS.md gallery
```

Each test runs as a subprocess with a fresh `BUBBLE_HOME` tempdir, stages synthetic packages via `tests/_common.stage_fake_package`, and emits a JSON result line that the runner aggregates into `tests/RESULTS.md`. Hermetic, offline, no PyPI access.

## Environment variables

- `BUBBLE_HOME` — default `~/.bubble`
- `BUBBLE_PYPI_INDEX` — default `https://pypi.org/simple`; refused at fetch time if not `https://`
- `BUBBLE_AUTOFAULT`, `BUBBLE_AUTOFETCH`, `BUBBLE_SCOPE`, `BUBBLE_VERBOSE` — meta-finder install-from-env knobs
- `BUBBLE_VERIFY` — set to `0` to opt out of verify-on-read (default on; vault drift refuses by default)
- `BUBBLE_ALLOW_SDIST` — set to `1` to opt in to sdist builds at vault-add (runs `setup.py` / build backend; default off)
- `BUBBLE_QUIET` — suppress non-error output

## CLI sovereignty defaults

`bubble run` and `bubble up` are vault-only by default — a vault miss raises rather than silently fetching. Network is opt-in via `--fetch` (or `BUBBLE_AUTOFETCH=1`).

`bubble vault get` refuses sdists by default. The refusal message names the alternative (install in a trusted venv, then `bubble vault import-venv`). Override with `--allow-sdist` and accept the trust boundary explicitly.

## Development notes

- Stdlib only. No package manager, no build system, no test framework — these are non-goals.
- The error loop in `run/runner.py` and the meta-finder's `_fault_to_pypi` both handle dynamic imports: catch `ModuleNotFoundError`, vault-fetch the missing dist, retry. Both record outcomes to `host.toml` via `host.record_failure` with kinds drawn from `FAILURE_KINDS`.
- `docs/integrity.md` was the design for vault tamper-resistance; it has been implemented. `vault_files` rows + `verify()` + verify-on-read in `meta_finder._lookup` and `shell._link_package`. Drift refuses; the refusal records.
- The four docs in `docs/` (`membrane.md`, `siblings.md`, `kithing.md`, plus the new `weft.md`) are part of the project's voice; read them before rewriting prose in the README or here.
- The substrate package (`bubble/substrate/`) is where new substrate handlers register. To add one: write the module, expose `is_available()` / `full_routing_implemented()` / `status()`, register in `substrate/__init__.py`. The router consults `is_implemented()`; the meta-finder's `_spec_for_alias` dispatches on `decision.actual`.

## `legacy/`

`legacy/bubble.py` (3,039 lines) and `legacy/bubble_cli.py` (the TTY wrapper) are the original monolith — schema v1, pip-driven, with `doctor`, `preflight`, an npm path, and a JS scanner that haven't been ported to `bubble/`. Preserved as exhibit, not maintained. See `legacy/README.md`.
