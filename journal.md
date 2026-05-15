# bubble journal

Build scratch pad. Terse, concrete, written during sessions not after.

---

## 2026-05-14 (afternoon) — keep flipped to vault-as-canonical

### what changed

The morning's keep stored each tree as a tar.gz snapshot. That made the vault a warehouse — copies of things lived there while the real things lived elsewhere on disk. Refresh-cadence was the user's problem.

Flipped the model. `~/.bubble/keep/<name>/` now holds the **live tree**. The original filesystem location becomes a symlink pointing into the vault. Edits at the symlink edit the vault bytes. No drift, no refresh.

- Capture moves the source into the vault and atomically swaps in the symlink.
- `bubble keep wire <name>` recreates the source-path symlinks from meta — the nuke-recovery verb.
- `bubble keep unwire <name>` removes the symlinks but leaves the vault tree.
- `bubble keep activate <name>` symlinks `bin/*` into `~/.local/bin/` so third-party binaries become PATH-visible while living in the vault.
- `bubble run <name>` now dispatches: `.py` → meta-finder; existing path → execv; bare name → resolve against keeps and execv.
- `bubble status` grew a keep section showing names, kinds, sizes.

Multi-file keeps (`capture_files`) handle scattered config — each file is symlinked back to its own original location independently. `.zshrc` and `.bashrc` are now files in the vault with the live `$HOME` paths being symlinks in.

### migration

Two existing keeps migrated:
- `odeon` — archive deleted, `~/odeon` copied into vault, `~/odeon` replaced with symlink. LFM2 weights (2.2 GB) now live in vault.
- `dotfiles` — archive deleted, `~/.zshrc` and `~/.bashrc` moved into vault, originals replaced with symlinks. The old `~/dotfiles` staging dir was removed. The `keep-dotfiles` helper function in `.zshrc` was deleted — vault is the live location, no snapshot/refresh needed.

### why the flip

Disk doubling was a non-tradeoff the morning design treated as a real cost. The actual cost is 60 GB/week of binaries scattered across `$PREFIX` and proot environments. Doubling 13 KB of dotfiles or 2 GB of weights against that is rounding error. The right answer is: vault is the home, surface paths are views.

### soft spots

- `.keepignore` is dropped. The old model excluded `__pycache__` / `*.pyc` and project-declared patterns. The new model only excludes those two defaults. Re-pullable artifacts like model weights now go into the vault unless the user pre-stages a clean tree. Symlinks-outward inside a keep are a possible escape hatch (a keep entry that's a symlink to an external path is included in the tree but its target stays external).

- `bubble run` non-py dispatch uses `os.execv` — argv[0] is the absolute target. Programs that key behavior off argv[0] basename will see `odeon` (correct); programs that grep `$0` for path components may see surprises.

- `wire` refuses if the source path already exists or is a symlink pointing somewhere else. Conservative on purpose — clobbering live state during nuke-recovery is the wrong default.

---

## 2026-05-14 — keep landed

### what got built

- **`bubble keep`** — second region of the vault: path-addressed directory keeps. `capture / list / show / restore / remove`. One tar.gz + meta.toml per name under `~/.bubble/keep/<name>/`. Stdlib `tarfile` + `gzip`, matching `bundle.py`'s codec choice.

- **`.keepignore`** — gitignore-style, one substring pattern per line, lives at the source root and travels with the project in version control. Default excludes `__pycache__`, `*.pyc`. CLI `--exclude` appends.

- **Size cap at 100 MB compressed** — refuses with `--force-large` escape hatch. The refusal message names `.keepignore` as the constructive alternative.

- **Two sha256 edges** — `tree_sha256` over deterministic walk of included content, `archive_sha256` over the tar.gz bytes. Restore re-checks the archive hash before extraction.

- **First real keep:** `bubble keep capture ~/odeon --name odeon` — 12 files, 13.7 KiB compressed (weights excluded via `.keepignore`). Round-trip restore to `~/odeon-restore-test` showed no content drift.

### why

The vault grew an asymmetry. It could tell you what your scripts touched but not what you touched. The framing that broke the design open: *the vault is where my important things go* — not what an agent happened to drop, but what I decided matters. Keep is that region.

### soft spots

- **`_bubble_reserved` updated** to include `project` and `keep`. Both were missing — `project` had landed earlier without the list being updated. The maintenance surface flagged in the 2026-05-11 entry was already overdue.

- **No dedup across captures.** Two keeps with the same paths but different names write two full archives. Fine for the scale the use case implies; would matter if keep grew toward backup-territory, which it shouldn't.

- **Restore overwrites with `--force` but doesn't reconcile.** It extracts what's in the archive; pre-existing files outside the archive are left in place. This is the right default for "restore my odeon directory after a nuke" but not for "diff my keep against current state." That second seam isn't built and probably shouldn't be — git is the tool for that.

### open questions

- **`bubble project ingest --keep` combo flag.** When a project has both a Python tree and non-Python artifacts (templates, data files, configs), the user shouldn't have to run two commands. The wiring is mechanical; deferred until the combo is actually asked for.

- **Keep + shell bundle.** A shell bundle is the portable artifact for a Python environment. Should a keep ride along inside a bundle when the user is preparing to move to another machine? The case is real for projects with non-Python state. Not built; not yet clear what the seam should look like.

---

## 2026-05-11 — shell activation wiring + field membrane seam

### what got built

- **`bubble <name>` zsh wrapper** in `~/.zshrc`: sources activate script, auto-starts service if `~/.bubble/shells/<name>/service` exists. PID written to `service.pid`. `bubble deactivate` kills via PID file first, `/proc` env scan fallback, then calls `bubble_deactivate` for env cleanup.

- **Prompt tag** `[<name>]─` via `${BUBBLE_SHELL:+([${BUBBLE_SHELL}])─}` in `configure_prompt`. Uses `promptsubst` — no PS1 hack, re-evaluated on each draw.

- **`_ACTIVATE_TMPL` patched** in the zipapp: saves/restores `_BUBBLE_OLD_PS1` for non-zsh shells. `bubble_deactivate` restores the prior PS1 on deactivation.

- **whisper service file** created at `~/.bubble/shells/whisper/service` — starts `server.py` in background when `bubble whisper` is invoked.

### the membrane seam (field side)

Field's `membrane/` package was built this session. From bubble's perspective: the vault is now the backing store for the OS-binary layer as well as Python imports. Field's membrane resolver does CAS lookup in `~/.bubble/vault/` before any network fetch. The vault path is exposed into the runtime namespace as a read-only bind mount — same bytes, different consumer.

Known mismatch: `membrane/census._scan_vault()` currently walks for `metadata.json` files which don't exist in the actual bubble vault layout. The real lookup needs `vault.db` (sqlite3 query against the `packages` table). Recorded in field's notebook; needs a fix before the integration is real.

### soft spots

- The `_bubble_reserved` list in the zsh wrapper must be kept in sync with bubble's actual subcommand set. If bubble adds a new subcommand (e.g. `field`, `membrane`, `watch`), the list needs updating or the new subcommand will be intercepted as a shell name. Not a bug currently; a maintenance surface.

- `bubble deactivate`'s `/proc` scan is a fallback that reads every `/proc/*/environ` in the system. Fine on a lightly-loaded Termux host; would be slow on a machine with thousands of processes. The PID file is the fast path; the scan is the correctness fallback. If the PID file is reliable (it is, as long as `bubble <name>` wrote it and the shell didn't crash), the scan almost never runs.

### open questions

- Vault integration for field membrane: needs `sqlite3` query against `vault.db` in `census._scan_vault()`. The `packages` table has `(name, version, wheel_tag, vault_path)` — that's the right row to expose as a host_path for bind mounting.

- Multi-env shared vault: if `star:kali` and `star:debian` both need `libssl`, can they both get it from the same vault entry without interference? The bind mount is read-only so the answer should be yes — but `ldconfig` inside each chroot may write cache files that collide. `/etc/ld.so.cache` is inside the chroot, not on the host, so they should be independent. Worth verifying.
