# keep

The vault is content-addressed for Python packages — bubble decides what goes there based on what scripts import. Keep is the other region: arbitrary user-chosen trees and files, named by the user, made canonical by the vault.

## the inversion

The first draft of keep stored each tree as a tar.gz under `~/.bubble/keep/<name>/`. That made the vault a snapshot warehouse — a place where copies of things lived, while the *real* things lived elsewhere on disk. The user had to choose between editing the live copy and refreshing the snapshot.

Keep flipped. The vault holds the live tree directly. The original filesystem location becomes a symlink pointing into the vault. Edits at the symlink edit the vault bytes — there is no second copy to refresh, no drift to chase.

## what gets captured

A directory or a set of files. The user names the keep (or lets the basename of the source decide). Capture copies the tree into `~/.bubble/keep/<name>/`, writes provenance to `~/.bubble/keep/.meta/<name>.toml`, then atomically replaces the source path with a symlink to the vault copy.

For multi-file captures — shell rc files, scattered config — each file is moved into the vault keep dir under its basename and symlinked back to its own original location independently. The meta records each `(source_path → vault_relative)` pair.

A `--no-symlink-back` flag is available if the user wants the source left in place (the vault still receives a copy). The default is to symlink: the inversion is the whole point.

## wire and unwire

Two verbs handle the symlink layer alone, separate from the bytes:

`bubble keep wire <name>` recreates the source-path symlinks from meta. This is the nuke-recovery verb — restore the vault directory from wherever it's backed up, then `wire` reconnects every captured tree to the place it was supposed to live.

`bubble keep unwire <name>` removes the source-path symlinks but leaves the vault tree intact. The vault is still the canonical home; the surface paths just stop pointing at it.

## activate and deactivate

When a keep has a `bin/` directory, `bubble keep activate <name>` symlinks each executable in it into `~/.local/bin/`, making the binaries PATH-visible. Deactivate removes those symlinks; the bin tree stays in the vault.

This is how third-party tools live in the vault rather than scattered across `$PREFIX`. The bytes are in `~/.bubble/keep/<name>/bin/`, the names are reachable through `~/.local/bin/`, and a filesystem nuke loses neither — restore the vault, run `activate`, the binaries are back.

## bubble run

`bubble run <target>` dispatches on shape:

- A `.py` script gets the meta-finder loading from the vault (the original bubble run path).
- A path that exists and is executable gets `execv`'d directly — sh, binary, whatever the kernel knows how to run.
- A bare name is resolved against vaulted keeps — `bin/<name>`, a single executable in `bin/`, or a top-level file with that name — and `execv`'d if found.

The dispatch means `bubble run odeon` is the same as typing the absolute path to the odeon binary, but driven by the keep registry.

## meta

Each keep's meta records:

- `name` — what the user filed it under
- `kind` — `dir` or `files`
- `captured_at` — when capture ran
- `symlinks` — list of `(source_path, vault_relative)` pairs for wire/unwire

No archive hash. No tree hash. The live tree on disk *is* the truth. If you want a verified backup, that's what a backup tool is for — keep doesn't pretend to be one.

## posture

Keep was added because the vault grew an asymmetry. Bubble could tell you what your scripts touched but not what *you* touched. The inversion goes further: the vault doesn't just track what you touched, it becomes the place those things live. A user who runs bubble for a year and then loses the filesystem doesn't lose the cache (which they could refetch) — they lose what they pointed at deliberately. Keep is the region that survives that.
