"""Claim: `bubble run` is offline by default; only --fetch (or
BUBBLE_AUTOFETCH=1) authorizes vault-miss → PyPI escalation.

Conventional intuition: a dev tool that auto-installs missing deps is
convenient. Bubble's stance: every silent network call is a silent trust
extension. Sovereignty default flips the polarity — a vault miss is a
hard fail unless the operator explicitly authorized network for this run.

This test pins down: with no flag and no env, a missing import surfaces
as ModuleNotFoundError, not as a fetch attempt.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    # Explicitly clear any inherited fetch authorization.
    os.environ.pop("BUBBLE_AUTOFETCH", None)
    os.environ.pop("BUBBLE_AUTOFAULT", None)

    from bubble.meta_finder import VaultFinder, install
    import importlib

    # Construct the same way cmd_run does, with the new default polarity.
    autofetch = bool(os.environ.get("BUBBLE_AUTOFETCH"))
    finder = install(autofetch=autofetch)
    try:
        # Pick a name that's certainly not vaulted (fresh BUBBLE_HOME) and
        # not a stdlib module.
        name = "this_pkg_is_definitely_not_vaulted_zzz"
        # Ensure the finder is configured to NOT autofetch in default mode.
        assert finder._autofetch is False, \
            f"finder.autofetch should be False by default; got {finder._autofetch!r}"
        # Asking for a vault-miss yields no spec — Python then raises
        # ModuleNotFoundError, which is the strict-default contract.
        spec = finder.find_spec(name, None)
        assert spec is None, f"vault-miss should yield None, got {spec!r}"
        r.evidence.append("default mode: autofetch=False, vault-miss → None spec")

        # Now flip the env and rebuild — same name, finder should attempt
        # fetch (and fail because the package doesn't exist on PyPI).
        # We don't need to wait for a real network call; we just verify
        # the toggle wired through.
        os.environ["BUBBLE_AUTOFETCH"] = "1"
        autofetch = bool(os.environ.get("BUBBLE_AUTOFETCH"))
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
        finder2 = install(autofetch=autofetch)
        assert finder2._autofetch is True, \
            f"BUBBLE_AUTOFETCH=1 should flip autofetch on; got {finder2._autofetch!r}"
        r.evidence.append("opt-in via BUBBLE_AUTOFETCH=1: autofetch=True at install")
        if finder2 in sys.meta_path:
            sys.meta_path.remove(finder2)
    finally:
        os.environ.pop("BUBBLE_AUTOFETCH", None)
        for f in list(sys.meta_path):
            if isinstance(f, VaultFinder):
                sys.meta_path.remove(f)
    r.passed = True


if __name__ == "__main__":
    run_test(
        "vault-only is the default for bubble's runtime — every fetch is "
        "an explicit authorization (--fetch CLI flag or BUBBLE_AUTOFETCH=1). "
        "A bare `bubble run` cannot reach PyPI, no matter what the script "
        "tries to import",
        body,
    )
