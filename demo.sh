#!/bin/sh
# demo.sh — kernel-isolated multi-version coexistence in one command.
#
# Runs demo.py against bubble.toml's [aliases] section: pydantic v1 and v2
# loaded side-by-side via the dlmopen-isolated substrate, both BaseModel
# surfaces invokable in one Python process. On hosts where libpython is
# resolvable for dlmopen, each alias gets its own link namespace and
# internal absolute imports resolve cleanly. On hosts without it, the
# router downgrades to in_process and records the reason to host.toml —
# the demo will surface a cross-namespace import collision; that's the
# substrate gap, not a bug. Install libpython for your Python (e.g.
# `apt-get install libpython3.13` on Debian/Kali) and re-run.
#
# Pass any extra args through (`./demo.sh -v` for verbose substrate-
# routing output).

set -eu

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found on PATH" >&2
    exit 1
fi

exec python3 -m bubble run demo.py --scope bubble.toml --fetch "$@"
