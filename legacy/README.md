# legacy

These two files were the whole of bubble at the start.

`bubble.py` carried everything: vault, scanner, ephemeral assembler, doctor, preflight, an npm path, a JS scanner. Schema v1, pip-driven. `bubble_cli.py` was the human face — a TTY wrapper that shelled out to `bubble.py` and rendered progress.

The package in `bubble/` is downstream of these. Most ideas survived; the mechanism changed. The vault is content-addressed by `(name, version, wheel_tag)` now; scanning, resolution, and assembly each live in their own module; the JSON Simple-API replaced the pip shell-out; demand-paged imports via `meta_finder.py` made the static scan→resolve→assemble pass optional. Features that didn't make the journey — `doctor`, `preflight`, the npm path, the JS scanner — wait here.

Nothing in `bubble/` imports from this directory. Preserved as exhibit, not maintained.
