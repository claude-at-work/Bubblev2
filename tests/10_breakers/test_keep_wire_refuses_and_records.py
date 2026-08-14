"""Claim: keep.wire() refusals are recorded to host.toml, not just raised.

wire() is nuke-recovery — it recreates the source-path symlinks a capture
left behind. Two ways it already refused before this test existed:
  - the vault-side tree is gone (path_missing)
  - something other than the expected symlink already occupies the
    source path (path_conflict)

Before this change both refusals raised and said nothing to host.toml —
the same "refusal but no record" gap vault drift closed for the vault
integrity edge. This test closes it for keep's filesystem edge.
"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble import keep, host, config

    config.ensure_dirs()

    src_root = Path(config.BUBBLE_HOME) / "project-src"
    src_root.mkdir(parents=True)
    (src_root / "main.py").write_text("print('hi')\n")

    meta = keep.capture(src_root, name="proj")
    assert src_root.is_symlink(), "capture should leave a symlink at the source"
    r.evidence.append(f"captured 'proj': {meta['symlinks']}")

    # ── path_conflict: unwire, then squat a plain file where the symlink goes ──
    keep.unwire("proj")
    assert not src_root.exists() and not src_root.is_symlink()
    src_root.write_text("squatter\n")  # not a symlink, not the vault tree

    before = len(host.known_failures())
    try:
        keep.wire("proj")
        assert False, "wire() should refuse when something occupies the source path"
    except FileExistsError:
        pass
    after_conflict = host.known_failures()
    assert len(after_conflict) == before + 1, \
        f"expected exactly one new failure, got {len(after_conflict) - before}"
    assert after_conflict[-1]["kind"] == "path_conflict", \
        f"expected path_conflict, got {after_conflict[-1]['kind']!r}"
    assert str(src_root) in after_conflict[-1]["target"]
    r.evidence.append(f"path_conflict recorded: {after_conflict[-1]}")

    src_root.unlink()  # clear the squatter for the next case

    # ── path_missing: the vault-side tree itself is gone ──
    dest = Path(config.BUBBLE_HOME) / "keep" / "proj"
    shutil.rmtree(dest)

    before = len(host.known_failures())
    try:
        keep.wire("proj")
        assert False, "wire() should refuse when the keep tree is missing"
    except FileNotFoundError:
        pass
    after_missing = host.known_failures()
    assert len(after_missing) == before + 1, \
        f"expected exactly one new failure, got {len(after_missing) - before}"
    assert after_missing[-1]["kind"] == "path_missing", \
        f"expected path_missing, got {after_missing[-1]['kind']!r}"
    r.evidence.append(f"path_missing recorded: {after_missing[-1]}")

    for kind in ("path_conflict", "path_missing"):
        assert host.is_known_kind(kind), f"{kind} missing from FAILURE_KINDS"

    r.passed = True


if __name__ == "__main__":
    run_test(
        "keep.wire() refusals (occupied source path, missing vault tree) "
        "record path_conflict / path_missing to host.toml instead of just raising",
        body,
    )
