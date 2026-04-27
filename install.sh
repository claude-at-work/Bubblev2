#!/bin/sh
# install.sh — clone, build, drop bubble somewhere on PATH.
#
#   ./install.sh                  # → ~/.local/bin/bubble  (no sudo, recommended)
#   ./install.sh /usr/local/bin   # → /usr/local/bin/bubble (may need sudo)
#   ./install.sh /opt/bubble/bin  # → custom prefix
#
# The build is pure stdlib zipapp — no third-party deps, no virtualenv,
# no setup.py. Drops a single self-contained executable; the vault lives
# at $BUBBLE_HOME (default ~/.bubble), independent of where this binary
# sits, so you can move either without touching the other.

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/.local/bin}"
NAME="${BUBBLE_BIN_NAME:-bubble}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found on PATH" >&2
    exit 1
fi

mkdir -p "$DEST"

python3 "$HERE/tools/build_pyz.py" -o "$HERE/bubble.pyz"

install -m 755 "$HERE/bubble.pyz" "$DEST/$NAME"

echo "installed: $DEST/$NAME"
echo "build sha: $(cut -d' ' -f1 < "$HERE/bubble.pyz.sha256")"

# Friendly PATH check — don't modify rc files; just tell the user.
case ":$PATH:" in
    *":$DEST:"*)
        ;;
    *)
        echo
        echo "note: $DEST is not on your PATH. Add it to your shell rc:"
        echo "  echo 'export PATH=\"$DEST:\$PATH\"' >> ~/.bashrc"
        echo "or run with the full path:  $DEST/$NAME --help"
        ;;
esac

echo
echo "try:"
echo "  $NAME --help"
echo "  $NAME probe          # writes ~/.bubble/host.toml"
echo "  $NAME vault list     # empty until you populate it"
