#!/usr/bin/env bash
set -euo pipefail

SRC_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bubble.pyz"
SRC="${1:-$SRC_DEFAULT}"
DEST="${BUBBLE_INSTALL_PATH:-/usr/local/bin/bubble}"

if [[ ! -f "$SRC" ]]; then
  echo "error: source artifact not found: $SRC" >&2
  echo "hint: pass the path explicitly: ./install.sh /path/to/bubble.pyz" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"

if [[ -f "$DEST" ]]; then
  src_hash="$(python3 - "$SRC" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
print(hashlib.sha256(p.read_bytes()).hexdigest())
PY
)"
  dest_hash="$(python3 - "$DEST" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
print(hashlib.sha256(p.read_bytes()).hexdigest())
PY
)"

  if [[ "$src_hash" == "$dest_hash" ]]; then
    echo "bubble already installed at $DEST (same artifact; no change)."
    exit 0
  fi

  ts="$(date +%Y%m%d%H%M%S)"
  backup="${DEST}.bak.${ts}"
  cp -p "$DEST" "$backup"
  install -m 0755 "$SRC" "$DEST"
  echo "replaced existing bubble at $DEST"
  echo "backup saved at $backup"
  exit 0
fi

install -m 0755 "$SRC" "$DEST"
echo "installed bubble to $DEST"
