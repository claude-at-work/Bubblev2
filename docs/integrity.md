# integrity

Bubble fetches code from PyPI and npm, unpacks it into the vault, and links from the vault into bubbles and shells. Two integrity questions arise on either side of "unpacks":

1. **Provenance** — did the bytes we received match what the index published?
2. **Tamper-resistance** — are the bytes we stored still the bytes we stored?

The first is already answered. `packages.sha256` records the artifact hash from PyPI's Simple API; `_download` refuses to write a file whose hash doesn't match, and a recent change made that check mandatory rather than optional. npm tarball URLs are now restricted to an https allowlist. Provenance is closed.

This document is the design for the second question. It is unbuilt at the time of writing.

## threat

A vault entry can drift after commit. The realistic causes are:

- attacker gains user-level write to `~/.bubble/vault/` and modifies a vaulted file to add a payload (a documented persistence pattern)
- accidental modification by the user (a stray `pip install` into the vault tree)
- filesystem corruption
- a bubble bug writing where it shouldn't

In every case the safe response is the same: refuse to use the entry, surface the drift, let the user decide. Re-fetch silently overwrites the user's filesystem and can mask network-level compromise; it is not a default behavior.

## shape

One new table, one read-only function, two CLI subcommands, three call sites. The shape mirrors what already exists in `bubble/vault/`.

```sql
CREATE TABLE vault_files (
    package    TEXT NOT NULL,
    version    TEXT NOT NULL,
    wheel_tag  TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    PRIMARY KEY (package, version, wheel_tag, rel_path),
    FOREIGN KEY (package, version, wheel_tag)
        REFERENCES packages(name, version, wheel_tag) ON DELETE CASCADE
);
CREATE INDEX idx_vault_files_sha256 ON vault_files(sha256);
```

Same FK family as `dependencies`, `modules`, `module_imports`, `top_level`. Same cascade behavior. Purely additive — no `ALTER TABLE`, no migration logic; bumping `SCHEMA_VERSION` to 3 is bookkeeping only.

The hash-during-commit folds into the existing tree walk in `store.commit()` that currently runs `_detect_native()`. One traversal, two outputs: `has_native` boolean and the `vault_files` rows.

`store.verify(package, version, wheel_tag) -> VerifyReport` is the read-only twin. Stat each file, compare `(size_bytes, mtime_ns)` to stored values, rehash only on disagreement. Returns pure data: matched, drifted, missing, extra files, and timing. Callers — `assemble.py`, `shell.py`, `meta_finder.py`, the CLI — decide what to do with the report.

Verify hooks attach immediately after the existing `is_under_vault` check at three sites: `bubble/run/assemble.py:assemble`, `bubble/run/shell.py:_link_package`, and `bubble/meta_finder.py:_lookup` (with a per-process `_verified` cache so each vault entry is checked once per run, not once per import).

## decisions

- **Stat fast-path, hash on disagreement.** Steady-state cost is a stat-storm in the microsecond range. Full rehash runs only when the filesystem reports a change. A determined attacker who resets mtime defeats this — `verify --strict` exists for that case.
- **Hard fail on drift, no auto-recovery.** Drift refuses the link and surfaces a report. `bubble vault verify --reset <name>` is the explicit, opt-in re-fetch. Silent overwrite is never a default.
- **Hardlinked packages check existence and size only, not content.** `bubble vault import-venv --hardlink` shares inodes with the source venv; modifications to either side propagate to both, so per-file content drift is expected. `is_hardlinked` is recorded in `packages.metadata` JSON (precedent: `imported_from` already lives there). Documented tradeoff, not a silent gap.
- **`vault_files.sha256` is integrity, `packages.sha256` is provenance.** Both columns persist; the docstrings make the distinction explicit.
- **Notifier is a caller concern.** `verify()` returns timing and counts. The caller decides whether to print. Honors `BUBBLE_QUIET`. Fires only when actual hashing occurred (fast-path hits stay silent).

## defaults

Sovereignty first. Verification is on by default in every path:

| path                       | default | opt-out                              |
| -------------------------- | ------- | ------------------------------------ |
| `bubble run`               | on      | `BUBBLE_VERIFY=0` or `--no-verify`   |
| `bubble shell add`         | on      | `--no-verify`                        |
| `meta_finder` import path  | on      | `BUBBLE_VERIFY=0`                    |
| `bubble vault verify`      | on      | (the command)                        |

First verification of a package version emits a one-line notice to stderr: package, file count, duration, and the opt-out variable. Subsequent runs use the stat fast-path and stay silent.

## out of scope

- **External scanner integration** (bandit, pip-audit, OSV-Scanner). Different concern: detecting *malicious* code, not *tampered* code. Belongs as a separate hook on `vault add`, not part of integrity.
- **Import-graph drift between versions.** A package gaining `subprocess` calls between releases is a real signal, but it's supply-chain shift, not vault integrity. Separate piece.
- **Content-addressed dedup.** Two packages with byte-identical files could share storage. The `idx_vault_files_sha256` index makes this possible later; not part of this scope.
- **Per-module capability model.** Python doesn't have one and one cannot be invented at the vault layer. See the parallel reasoning about in-process capability limits.
- **`bubble doctor` integration.** Doctor lives only in the legacy `bubble.py` monolith and has not been ported to `bubble/cli.py`. When/if it ports, verify summarization belongs there. Until then, `bubble vault verify` is the explicit entry point.

## sequence

1. Append the `vault_files` table to `SCHEMA` in `bubble/vault/db.py`. Bump `SCHEMA_VERSION` to 3.
2. Replace `_detect_native` walk in `store.commit()` with a single tree walk that returns the per-file rows; insert into `vault_files`; derive `has_native` from the result.
3. Add `store.verify(package, version, wheel_tag) -> VerifyReport`. Pure data return.
4. Add `bubble vault verify [name]` and `bubble vault rehash [name|--all]` to `bubble/cli.py`.
5. Wire verify into `assemble.py`, `shell.py:_link_package`, and `meta_finder.py:_lookup` at the same line as the existing `is_under_vault` check. Add per-process `_verified` cache in the meta-finder.
6. Add the notifier helper. Caller-side; honors `BUBBLE_QUIET`; suppresses on fast-path hits.

Roughly 120 lines of new code. No external dependencies. The integrity check is not a new architectural layer; it is the existing per-package fact-table-plus-FK pattern continuing one more step.
