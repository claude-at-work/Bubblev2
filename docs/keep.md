# keep

The vault was content-addressed Python packages. Keep is the second region: arbitrary user-chosen directory trees, named, captured as a single archive, restorable on a fresh filesystem.

The two regions answer different questions. The package vault answers *what does this script import?* — its contents are derivable; a runtime put them there. Keep answers *what did I decide matters?* — its contents are not derivable from anything else, and nothing will put them back if they are lost.

## what gets captured

A directory tree. The user names it. The capture writes one tar.gz under `~/.bubble/keep/<name>/tree.tar.gz` and a `meta.toml` next to it. That is the whole layout.

What is excluded by default: `__pycache__`, `*.pyc` — never useful to preserve. What else gets excluded comes from the project itself: a `.keepignore` file at the source root, gitignore-style but simple — one substring pattern per line, lines starting with `#` are comments, a path matches if any pattern is a substring of the path relative to the source root. The CLI `--exclude PATTERN` flag appends to whatever `.keepignore` declared.

The intent of `.keepignore` is for the project to declare its own non-essentials in version control next to its code. Weights re-pullable from a registry, runtime state, transcripts that grow forever — these don't belong in a capture meant to survive a filesystem nuke.

## the size cap

Captures over 100 MB compressed refuse. The user passes `--force-large` to accept, or adds patterns to `.keepignore`. The point of keep is that the user picks what's important; silent inflation would defeat that. A capture that refuses is a capture that made the user look.

## two integrity edges

`tree_sha256` — hash over the included file contents and their relative paths, in deterministic walk order. Stable across machines as long as the exclude set is the same. This is the cryptographic edge between *what was on disk* and *what got captured*.

`archive_sha256` — hash over the tar.gz bytes themselves. Re-checked at restore-time before any extraction. This is the cryptographic edge between *what got written* and *what is being read back*. A mismatch refuses the restore.

## restore

`bubble keep restore <name>` extracts back to the original source path recorded in `meta.toml`. `--target PATH` overrides. A non-empty target refuses unless `--force` is passed. Same posture as the size cap: a destructive restore is a thing the user has to mean.

## what keep is not

Keep is not version control. It captures one tree at one moment. Two captures with the same name overwrite (with `--overwrite`); there is no history, no diff, no merge. The name space is flat. If the user wants history they have git; keep is the layer below that, for the trees git is not tracking — projects without commits, configuration, working directories whose .git is itself the thing being preserved.

Keep is not a backup system. It does not schedule, does not deduplicate across captures, does not sync to a remote. It writes a single tar.gz to the local vault home. What the user does with the vault home is the user's call — `bubble shell bundle` is a separate seam for portability, and a keep can ride along inside a shell bundle if the user wants it to.

## posture

Keep was added because the vault grew an asymmetry it shouldn't have had: bubble could tell you what your scripts touched, but not what *you* touched. A user who runs `bubble` for a year and then nukes the filesystem should not lose what they pointed at deliberately. The cost was small — one module, five verbs, a stdlib tar.gz codec — and the asymmetry it closes is the difference between a cache and an archive.
