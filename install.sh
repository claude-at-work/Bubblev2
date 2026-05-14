#!/bin/sh
# install.sh — build bubble and drop it somewhere on PATH.
#
#   ./install.sh                  # → ~/.local/bin/bubble  (no sudo, recommended)
#   ./install.sh /usr/local/bin   # → /usr/local/bin/bubble (may need sudo)
#   ./install.sh /opt/bubble/bin  # → custom prefix
#   ./install.sh --update         # git pull upstream first, then install
#
# Re-running this script updates an existing install in place: the build
# is content-addressed (same source bytes → same sha256), so we compare
# the freshly-built .pyz against whatever is already at the destination
# and report install / update / already-up-to-date accordingly. The vault
# at $BUBBLE_HOME (default ~/.bubble) is never touched, so updating the
# binary is independent of the data it serves.
#
# --update fetches the tracking branch and fast-forwards before building,
# so every merged PR on the remote lands in your install on the next run.
# Refuses to pull over uncommitted changes or a diverged branch.
#
# The build is pure stdlib zipapp — no third-party deps, no virtualenv,
# no setup.py. Drops a single self-contained executable.

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"

UPDATE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --update|-u)
            UPDATE=1
            shift
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "error: unknown flag: $1" >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
done

DEST="${1:-$HOME/.local/bin}"
NAME="${BUBBLE_BIN_NAME:-bubble}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found on PATH" >&2
    exit 1
fi

# --update: fast-forward HERE to its tracking branch before we build.
# We trust git's own semantics — any merged PR on the remote shows up as
# new commits on the tracking branch, which is what we pull.
if [ "$UPDATE" = "1" ]; then
    if ! command -v git >/dev/null 2>&1; then
        echo "error: --update requires git on PATH" >&2
        exit 1
    fi
    if ! git -C "$HERE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "error: --update requires the install script to live in a git checkout" >&2
        exit 1
    fi
    BRANCH="$(git -C "$HERE" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
    if [ -z "$BRANCH" ]; then
        echo "error: --update needs a branch checkout (HEAD is detached)" >&2
        exit 1
    fi
    UPSTREAM="$(git -C "$HERE" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
    if [ -z "$UPSTREAM" ]; then
        echo "error: branch '$BRANCH' has no upstream configured" >&2
        exit 1
    fi
    # Reject uncommitted changes — git pull --ff-only would still succeed
    # but it makes the audit trail murky if the build picks up dirty bytes.
    if ! git -C "$HERE" diff --quiet || ! git -C "$HERE" diff --cached --quiet; then
        echo "error: working tree has uncommitted changes; commit, stash, or discard before --update" >&2
        exit 1
    fi
    echo "fetching $UPSTREAM..."
    git -C "$HERE" fetch --quiet
    LOCAL="$(git -C "$HERE" rev-parse HEAD)"
    REMOTE="$(git -C "$HERE" rev-parse "$UPSTREAM")"
    BASE="$(git -C "$HERE" merge-base HEAD "$UPSTREAM")"
    if [ "$LOCAL" = "$REMOTE" ]; then
        echo "already on the latest $UPSTREAM ($(printf '%s' "$LOCAL" | cut -c1-12))"
    elif [ "$LOCAL" = "$BASE" ]; then
        AHEAD="$(git -C "$HERE" rev-list --count "$LOCAL..$REMOTE")"
        echo "pulling $AHEAD new commit(s) from $UPSTREAM..."
        git -C "$HERE" pull --ff-only --quiet
    elif [ "$REMOTE" = "$BASE" ]; then
        echo "local $BRANCH is ahead of $UPSTREAM; nothing to pull"
    else
        echo "error: local $BRANCH and $UPSTREAM have diverged; resolve manually" >&2
        exit 1
    fi
fi

# sha256 of a file, or empty string if absent / no hasher available.
# Handles sha256sum (linux) and shasum -a 256 (macos).
_sha256_of() {
    [ -f "$1" ] || return 0
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

mkdir -p "$DEST"

# Capture pre-install state so the report distinguishes install / update / no-op.
OLD_SHA="$(_sha256_of "$DEST/$NAME" || true)"

python3 "$HERE/tools/build_pyz.py" -o "$HERE/bubble.pyz"

NEW_SHA="$(cut -d' ' -f1 < "$HERE/bubble.pyz.sha256")"

if [ -z "$OLD_SHA" ]; then
    ACTION="install"
elif [ "$OLD_SHA" = "$NEW_SHA" ]; then
    ACTION="noop"
else
    ACTION="update"
fi

install -m 755 "$HERE/bubble.pyz" "$DEST/$NAME"

case "$ACTION" in
    install)
        echo "installed: $DEST/$NAME"
        ;;
    update)
        echo "updated:   $DEST/$NAME"
        echo "previous:  $(printf '%s' "$OLD_SHA" | cut -c1-12)"
        ;;
    noop)
        echo "already up to date: $DEST/$NAME"
        ;;
esac
echo "build sha: $NEW_SHA"

# Friendly PATH check — don't modify rc files; just tell the user.
ON_PATH=1
case ":$PATH:" in
    *":$DEST:"*) ;;
    *) ON_PATH=0 ;;
esac

if [ "$ON_PATH" = "0" ]; then
    echo
    echo "note: $DEST is not on your PATH. Add it to your shell rc:"
    echo "  echo 'export PATH=\"$DEST:\$PATH\"' >> ~/.bashrc"
fi

# Run setup unless explicitly skipped — fills the vault from every
# site-packages this Python knows about, hardlinking by default. Safe to
# re-run; idempotent. BUBBLE_SKIP_SETUP=1 ./install.sh skips it. Also
# auto-skipped when the binary didn't change (no-op re-runs stay quiet);
# rescan an existing install with `$NAME setup`.
if [ "${BUBBLE_SKIP_SETUP:-0}" = "0" ] && [ "$ACTION" != "noop" ]; then
    echo
    if [ "$ACTION" = "install" ]; then
        echo "running first-time setup (vault scan)..."
    else
        echo "rescanning vault for the updated build..."
    fi
    "$DEST/$NAME" setup || {
        echo "setup hit an error; bubble itself is installed and runnable." >&2
        echo "you can re-run setup any time:  $NAME setup" >&2
    }
fi

echo
echo "ready. try:"
if [ "$ON_PATH" = "0" ]; then
    echo "  $DEST/$NAME --help"
    echo "  $DEST/$NAME vault list"
else
    echo "  $NAME --help"
    echo "  $NAME vault list"
fi
