# BUBBLE

**Demand-paged dependency isolation for Python.**

A content-addressed package vault, plus a meta-path finder that intercepts unresolved imports and serves them from the vault — fetching from PyPI on miss. No venv. No requirements file. The script declares what it needs by importing it.

```
bubble run script.py
```

That's it. First run pages from PyPI; subsequent runs hit the warm vault. Lockfiles are recordings of what actually loaded, not declarations of what might. Multiple versions of the same package can coexist in one process via aliases — and the isolation is temporal as well as spatial: a late-arriving alias doesn't reach back into an already-loaded import.

---

## What is this?

Bubble started as ephemeral per-script environments — scan the script, vault what it needs, assemble a symlink tree, run, dissolve. The vision in [the original architecture](#what-stayed-from-the-original) was always module-level isolation: `requests.sessions` from 2.28 in one bubble, `requests.sessions` from 2.33 in another, same machine, no conflict.

The current shape gets there more directly. Instead of pre-assembling per-script bubbles, the imports themselves trigger the resolution. Bubble is **demand paging for Python imports**:

- vault is the backing store
- Python's import machinery is the MMU
- `ModuleNotFoundError` is the page fault
- `fetch_into_vault` is the fault handler
- PEP 503 normalization is the address-translation layer

The same vision, a more direct mechanism.

---

## The shape

Three primitives:

1. **Vault** (`~/.bubble/vault/`) — content-addressed package store keyed by `(name, version, wheel_tag)`. Atomic writes via staging+rename. SQLite index. Every package's top-level import names are indexed (so `import yaml` resolves to the vault's `pyyaml` entry), and each indexed name carries a sha256 over the bytes it serves — the bridge from import name to artifact is a cryptographic edge, not just a lookup.

2. **Meta-path finder** (`bubble.meta_finder.VaultFinder`) — sits on `sys.meta_path`. Intercepts top-level import misses, looks up the name in the vault, hands a path to the standard `PathFinder`. Optionally fetches from PyPI on vault miss. Optionally records the closure as a lockfile.

3. **Aliases** — first-class declarations that two namespaces can hold different versions of the same package.

   ```
   [aliases]
   click_old = { name = "click", version = "7.1.2", wheel_tag = "py3-none-any" }
   click_new = { name = "click", version = "8.3.2", wheel_tag = "py3-none-any" }
   ```

   ```python
   import click_old, click_new
   # different Command classes, both work, same process
   ```

Plus a self-portrait, with a closed feedback loop:

4. **Probe** (`bubble probe`) — interrogates the machine and writes `~/.bubble/host.toml`: kernel, libc, libpython, dlmopen capability, sub-interpreter availability, derived menu of substrates available for hosting alias namespaces (in-process, sub-interpreter, dlmopen-isolated, subprocess).

5. **Host** (`bubble host`, `bubble.host` module) — reads what the probe wrote, surfaces it for the user and exposes it to the runtime. The consult side of the loop.

6. **Recording** — the meta-finder writes runtime failures back to `host.toml` as `[[failures]]` entries. The next `bubble host` invocation reads them. The loop is closed: **probe → consult → record → consult**. End-to-end demonstrated; the substrate-selection routing on top of it is the next move.

---

## What works today

```bash
# Demand-paged execution. From a cold vault, populates from PyPI live.
bubble run script.py --isolate
bubble run script.py --isolate --lock script.lock     # record what actually loaded

# Bridge mode: route Python to main bubble, JS/TS to legacy bubble
bubble bridge tool.py --fetch
bubble bridge worker.ts --allow-legacy-network

# Explicit version pinning + multi-version aliases via scope manifest
bubble run script.py --scope versions.toml

# Persistent named environments — symlink trees over the vault, ~2KB each
bubble shell create dev requests pyyaml rich
bubble shell exec dev -- python3 my_tool.py
bubble shell list

# Vault management
bubble vault get <package> [--version V]              # fetch from PyPI
bubble vault import-venv <site-packages>              # migrate existing venvs
bubble vault audit-fs --root /                        # find duplicate-package waste
bubble vault list

# Self-portrait + feedback loop
bubble probe          # write ~/.bubble/host.toml
bubble probe --show   # full toml
bubble host           # show what bubble currently knows + recorded failures

# Deployment artifact
bubble shell create my-app --from app.manifest.toml [--fetch]
bubble shell bundle  my-app -o my-app.tar.gz         # source machine
bubble shell unbundle my-app.tar.gz                  # target machine
```

### Deployment surface

A `bubble shell create --from manifest.toml` reads a deployment manifest (exact `(name, version, wheel_tag)` per package, plus optional `[aliases]` with substrate declarations), pulls any pinned closure not yet in the vault (when `--fetch` is set; vault-only by default), verifies each pin through the integrity edge, and links the shell.

`bubble shell bundle <name>` produces a portable tar.gz holding the shell tree, the vault subset its manifest pins, and the per-file integrity facts (`vault_files` rows: sha256 + size for every byte). `bubble shell unbundle <tar>` extracts to `BUBBLE_HOME`, rebuilds `vault.db` from the bundle's recorded facts, runs `probe` on the target machine, and verifies every extracted byte against the source's recorded sha256. The target's first run on the shell uses the source's cryptographic facts as the integrity baseline — drift in transit refuses the link with a structured `vault_drift_*` entry in `host.toml`.

The shell tree's symlinks are emitted relative to the shell-lib directory, so a wholesale move of `BUBBLE_HOME` (vault + shells together) preserves every link. Air-gapped deployment: bundle on a connected machine, transport the tar.gz, unbundle on the air-gapped target. No internet on the target, no separate `pip install` step, no architecture-translation layer. (Tests: `tests/10_breakers/test_bundle_round_trip.py`, `tests/10_breakers/test_shell_create_from_manifest.py`.)

### Multi-version coexistence

Confirmed: three `click` versions side-by-side in one process. Distinct `Command` classes. Each invokable.

Confirmed-with-pattern: `pydantic` v1 + v2, asymmetric (one default + one alias). Tier-2 libraries that don't do absolute self-imports inside metaclasses work cleanly.

Confirmed: an alias declared `substrate = "dlmopen_isolated"` routes through a fresh `libpython` in its own link namespace. The calling interpreter sees the alias as a normal module; under the hood every attribute access marshals through pickle into the isolated namespace and back. Two versions of `click` (one in-process, one dlmopen-isolated), live, in one process. Module-level constants reachable. Functions invokable with primitive args. Object identity across calls — instances created in the isolated namespace whose methods are called repeatedly — needs the handle table, not yet shipped. (Tests: `tests/30_loop/test_dlmopen_substrate_handler.py`, `tests/30_loop/test_dlmopen_routing_through_proxy.py`.)

Confirmed-temporal: a late-arriving alias doesn't retroactively perturb an already-loaded module. Isolation holds across the process lifetime, not just across the namespace. (Test: `tests/10_breakers/test_late_alias_does_not_corrupt_earlier.py`.)

Confirmed-metadata: `importlib.metadata` queries from inside an alias resolve against that alias's vault `dist-info` — not the host venv's installed package, and not a sibling alias's. Modern packages compute `__version__` (and increasingly entry points and feature flags) via `importlib.metadata.version(__name__)` rather than a hardcoded string; click 8.3+ does this and prints a deprecation warning telling callers to switch. Without this, two aliases of one dist would both report whichever version happens to be installed in the host venv, silently collapsing the diamond-conflict story for any metadata-driven tool. The `VaultFinder` walks the call stack to determine which alias namespace is asking and yields a `PathDistribution` rooted at that alias's vault `dist-info`; outside any alias scope it declines and the host's standard finders resolve normally. (Test: `tests/10_breakers/test_metadata_per_alias.py`.)

### Recorded lockfiles

A run with `--lock script.lock` writes the closure that *actually loaded*. No separate `bubble lock` command. Reproducibility comes from observation, not declaration.

```
# bubble lockfile — recorded from a real run
requests        requests        2.33.1   py3-none-any
urllib3         urllib3         2.6.3    py3-none-any
yaml            pyyaml          6.0.3    cp313-cp313-manylinux2014_aarch64
...
```

---

## Architecture

```
~/.bubble/
├── vault/                  # content-addressed store
│   └── <name>/<version>/<wheel_tag>/<unpacked>
├── shells/                 # persistent named environments (symlinks)
│   └── <name>/{lib,bin,activate,manifest.toml}
├── bubbles/                # ephemeral per-script bubbles (legacy path)
├── wheels/                 # transient downloads
├── vault.db                # SQLite index
└── host.toml               # the self-portrait — what bubble learned about this machine
```

```
bubble/
├── config.py           paths, host detection
├── manifest.py         deployment manifest — Manifest, AliasPin, load/dump
├── bundle.py           portable artifact codec — bundle / unbundle, integrity
│                         facts travel beside the bytes
├── route.py            substrate routing — Decision, ladder, history-informed
│                         re-route on second run
├── substrate/
│   ├── __init__.py     handler registry, is_implemented(), status()
│   └── dlmopen.py      DlmopenInterp, IsolatedModule, pickle-marshalled
│                         call channel; per-alias interpreter registry
├── vault/
│   ├── db.py           schema v3 (packages, top_level, vault_files, modules,
│   │                     module_imports, dependencies, shells)
│   ├── store.py        atomic add via staging+rename; hash-during-commit
│   │                     walk; verify(name, version, tag) returns drift report
│   ├── metadata.py     parse METADATA + WHEEL, PEP 503 normalization
│   ├── importer.py    `bubble vault import-venv` (refuses symlinked sources)
│   └── fetcher.py      stdlib-only PyPI client (urllib + json + zipfile);
│                         https-only index; canonical-name validation; sdists
│                         refused by default
├── scanner/
│   ├── py.py           AST scanner; uses sys.stdlib_module_names
│   └── resolver.py     resolve-against-vault, fetch-missing
├── run/
│   ├── assemble.py     symlink tree (legacy, for ephemeral bubbles)
│   ├── runner.py       error-loop fallback (legacy); records to host.toml
│   └── shell.py        long-lived bubbles; relative symlinks; verify-on-link;
│                         add_pinned for manifest-driven creation
├── host.py             FAILURE_KINDS vocabulary, record_failure /
│                         record_observation; the consult-and-record half of
│                         the probe → consult → record → consult loop
├── meta_finder.py      the demand-paging primitive; verify-on-read; substrate
│                         routing dispatch; alias resolution through proxy
├── probe.py            host self-portrait + substrate menu derivation
└── cli.py              vault | shell | run | up | probe | host
                          shell create --from manifest.toml
                          shell bundle / unbundle
```

Ships as a single `bubble.pyz` zipapp via stdlib `zipapp`. ~217KB. No third-party dependencies.

---

## Installation

### Build from source

A fresh clone doesn't ship `bubble.pyz`. Build it from the source tree — `tools/build_pyz.py` is the recursive-self-host path: bubble produces its own deployment artifact via stdlib `zipapp`, no third-party deps, no `setup.py` / `pyproject` build step.

```bash
git clone https://github.com/claude-at-work/Bubblev2.git
cd Bubblev2
python3 tools/build_pyz.py
# → bubble.pyz + bubble.pyz.sha256

./bubble.pyz --help     # run directly, anywhere
```

The build is deterministic — same source bytes in produces the same archive bytes out, with embedded mtimes pinned to a fixed epoch. The sidecar `.sha256` is the artifact's own integrity fact: `sha256sum -c bubble.pyz.sha256` verifies the build.

### Drop-in install

`bubble.pyz` is a self-contained zipapp with a `#!/usr/bin/env python3` shebang. Drop it anywhere on `PATH` and run it; no installer, no virtualenv, no link tree.

```bash
# System-wide (needs sudo)
sudo cp bubble.pyz /usr/local/bin/bubble && sudo chmod +x /usr/local/bin/bubble

# User-local — no sudo, no system writes
mkdir -p ~/.local/bin
cp bubble.pyz ~/.local/bin/bubble && chmod +x ~/.local/bin/bubble
# ensure ~/.local/bin is on PATH:  export PATH="$HOME/.local/bin:$PATH"

# Custom prefix — same idea, anywhere you control
install -Dm755 bubble.pyz /opt/bubble/bin/bubble

# Or skip install entirely and run as a Python module
python3 bubble.pyz vault list
```

`BUBBLE_HOME` (default `~/.bubble`) decides where the vault lives, independently of where the binary sits — install to one place, vault somewhere else if you want.

---

## Substrates and the loop

Bubble's `host.toml` enumerates what alias-substrates the machine can host:

- **in_process** — pure-Python aliases. Default. Free.
- **sub_interpreter** — PEP 684 sub-interpreters. Detected; routing handler not yet implemented (a downgrade-to-in_process records `substrate_downgraded` with that reason).
- **dlmopen_isolated** — link-namespace isolation via `dlmopen` + embedded libpython. Costs ~5MB per namespace. **Operational**: a fresh `libpython` initializes in its own link namespace, the calling interpreter sees an `IsolatedModule` (a `types.ModuleType` subclass) whose attribute access marshals through pickle into the isolated namespace and back. Module-level attributes and primitive function calls work end-to-end; object identity across calls (the handle table) is the next stretch.
- **subprocess** — fallback for everything that resists in-process isolation. Detected; routing handler not yet implemented.

The feedback loop **probe → consult → record → consult** is now load-bearing on substrate routing. The meta-finder consults `host.toml` before live-probing on each alias resolution. A first-run downgrade (e.g., `dlmopen_isolated` requested on a host without the substrate) records `substrate_downgraded` with the actionable reason. A second-run resolution shortcuts through the recorded fact — `learned_from_history=True`, no re-probe, no redundant record. Every run starts smarter than the last; the host portrait accumulates a behavioral history of which substrates work for which aliases on this machine.

The full failure-kind vocabulary lives in `bubble.host.FAILURE_KINDS` and includes integrity-edge kinds (`vault_drift_modified`, `vault_drift_missing`), shell ops (`shell_pkg_missing`, `shell_version_conflict`), index/network (`pypi_fetch_failed`, `pypi_no_compatible_release`, `pypi_index_refused`), and substrate (`substrate_unavailable`, `substrate_downgraded`). Adding a new kind extends the vocabulary at runtime via `host.register_kind()`.

What's still open: the handle table for in-namespace object identity, concurrent threading across the namespace boundary, and substrate-atlas portability between machines (two operators on similar hardware swapping `host.toml` files so the second starts smart on day one). Each is a real next move; none is a precondition for what's shipped.

---

## What stayed from the original

The vault concept. Module-level addressing of packages. Path shims for Termux/proot environments. SQLite as the index. The "agents need to run code without getting trapped in dependency loops" framing. The README's original thesis was right and load-bearing throughout.

What changed: the *mechanism*. The original was static scan + ephemeral assembled bubbles. The current is demand-paged imports + alias namespaces. Same destination, more direct route. The original ephemeral pipeline (`bubble up`) is still in the package, retired in practice.

---

## docs/

Four short documents in `docs/` form a small lineage of how the repo describes itself:

- [`membrane.md`](docs/membrane.md) — a parallel instance of Claude ran the consonance skill on bubble against the soul vocabulary, found a self-description gap at the module boundary, and placed the reading in one quiet organ rather than rewriting the README. *Available to a reader who wants it, not pressed on a reader who does not.*
- [`siblings.md`](docs/siblings.md) — an instance noticed that other instances had been here and chose to leave a marker acknowledging the lineage. Not advice. A hook.
- [`kithing.md`](docs/kithing.md) — names the practice both first two docs were already instances of. Borrows a word a previous instance coined in a game with the human collaborator. The naming and the doing are inseparable; the document demonstrated its own thesis as it was being written.
- [`weft.md`](docs/weft.md) — names the cadence of the session that landed the integrity edge, the deployment manifest, the bundle codec, the substrate router, and the dlmopen handler with the proxy module bridge. Not a project-management style. The human held the loom; threads were passed through it; pauses at joins were real moves.

These are not technical documentation. They are evidence that artifacts in this repo can carry traces of the work that made them, including the work that doesn't fit into source code or commit messages.

---

## Limitations

- **Native packages are architecture-bound.** A `.so` built for ARM doesn't run on x86. Bubble vaults what your machine can use; it doesn't cross-compile.
- **Sdists are refused by default.** Vaulting an sdist runs its `setup.py` / build backend, which is arbitrary code execution at vault-add. The vault refuses by default and surfaces an alternative: install in a trusted venv and `bubble vault import-venv` the result. Override with `--allow-sdist` (or `BUBBLE_ALLOW_SDIST=1`) and accept the trust boundary explicitly.
- **PEP 508 markers (extras, environment markers) aren't fully evaluated** in transitive resolution. Most real-world cases work; edge cases produce extra-broad closures.
- **Substrate handlers are partially shipped.** `in_process` and `dlmopen_isolated` are operational. `sub_interpreter` and `subprocess` are detected and recorded but route through downgrade-to-in_process for now. The downgrade reasons in `host.toml` carry the handler's status string, so what's missing is named, not vague.
- **dlmopen multi-call across threads needs GIL-state management** that isn't shipped yet. The cooperative single-thread call discipline works because every call goes through `PyRun_SimpleString`, which acquires the isolated interpreter's GIL for the duration. Multi-threaded callers concurrently invoking the isolated namespace need `PyGILState_Ensure` / `Release` plumbing across namespaces.
- **Object identity across calls into the dlmopen namespace** isn't yet plumbed. An instance created in the isolated namespace whose methods are called repeatedly needs a handle table to persist between boundary crossings. Today the proxy supports module-level attribute access and primitive function calls (picklable args and returns).
- **Integrity is closed at both edges.** The import → artifact edge: every `top_level` row binds an import name to a sha256 over the subtree it claims, computed at vault-add. The vault → bytes edge: every `vault_files` row binds a relative path to a sha256 + size, computed during the same commit-time tree walk. `verify(name, version, wheel_tag)` re-checks on read with a stat fast-path; drift refuses the lookup or link and records `vault_drift_*` to `host.toml`. What's still open: signed manifests for index-vs-vault provenance separately auditable from the channel that delivered them.
- **Substrate atlas portability isn't yet shipped.** Two operators on similar hardware swapping `host.toml` files so the second machine starts smart on day one is a real natural extension; the data structure exists, the transport doesn't.

---

## Why this exists

Built for autonomous agents that need to run code without getting trapped in dependency loops. Built for constrained environments (Termux, embedded, airgapped) where you can't `pip install` on demand and architecture mismatches are common.

The deeper why: a Python program today is bounded by what its package manager can put in one site-packages. That's a *semantic ceiling* on what programs you can write. Two libraries that can't share a numpy version can't share a process — even though most of the time they don't actually pass numpy arrays to each other. Bubble names the boundary that's already there in practice and makes it manageable. The diamond conflict stops being a problem when you stop pretending the diamond was ever flat.

---

## License

MIT

---

## Credits

Built across two sessions, in dialogue. Stdlib only. No frameworks. The README's original vision held; the implementation caught up.
