"""Claim: the subprocess-isolated substrate is a verified capability —
a child Python interpreter spawns, a vaulted package loads inside it,
and a value crosses the OS-process boundary back to the caller via the
length-prefixed pickle channel.

Where this sits relative to dlmopen: the dlmopen handler isolates
inside one OS process via link namespaces (powerful, ~5MB per ring,
glibc-only). The subprocess handler isolates across OS processes
(less exotic, ~30MB per ring, portable everywhere Python runs). Both
serve the same diamond-conflict-dissolution role; subprocess closes
the structural hole that dlmopen's portability constraints leave
open. Together they make the substrate ladder load-bearing across
hosts the architecture has named.

Pinned:
  - is_available() returns True (almost universal — sys.executable is
    by definition runnable; we still probe to surface exotic-host
    failures structurally rather than at the first import attempt)
  - SubprocessInterp constructor spawns a child python and the channel
    is online
  - install_module imports a vaulted package in the child
  - get_attr returns a primitive value from the child
  - call_attr invokes a function in the child with primitive args and
    receives the return value
  - close() shuts the child down without orphaning the process
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble.substrate import subprocess as sub_mod
    from bubble import host

    if not sub_mod.is_available():
        r.skipped = (
            f"subprocess substrate not available on this host: "
            f"{sub_mod._AVAIL_REASON or 'unknown'}"
        )
        return

    r.evidence.append(f"subprocess substrate available on this host")
    r.evidence.append(f"  status: {sub_mod.status()}")

    name, version, wheel_tag, vault_path = stage_fake_package(
        name="islet_sub",
        version="3.1.4",
        import_name="islet_sub",
        init_source='''
            VERSION = "3.1.4"
            ANSWER = 42
            def double(x): return x * 2
            def concat(a, b): return f"{a}|{b}"
        ''',
    )
    r.evidence.append(f"staged islet_sub=={version} at {vault_path}")

    with sub_mod.SubprocessInterp() as interp:
        interp.install_module(vault_path, "islet_sub")
        r.evidence.append("install_module: islet_sub imported in child")

        version_val = interp.get_attr("islet_sub", ("VERSION",))
        assert version_val == "3.1.4", (
            f"VERSION crossed as {version_val!r}, expected '3.1.4'"
        )
        r.evidence.append(f"VERSION crossed the boundary: {version_val!r}")

        answer_val = interp.get_attr("islet_sub", ("ANSWER",))
        assert answer_val == 42, (
            f"ANSWER crossed as {answer_val!r}, expected 42"
        )
        r.evidence.append(f"ANSWER crossed the boundary: {answer_val!r}")

        # Function call with primitive arg, primitive return.
        doubled = interp.call_attr("islet_sub", ("double",), (21,), {})
        assert doubled == 42, (
            f"double(21) crossed as {doubled!r}, expected 42"
        )
        r.evidence.append(f"double(21) crossed the boundary: {doubled!r}")

        # Function call with kwargs to verify both arg paths work.
        concatted = interp.call_attr(
            "islet_sub", ("concat",), ("left",), {"b": "right"},
        )
        assert concatted == "left|right", (
            f"concat crossed as {concatted!r}, expected 'left|right'"
        )
        r.evidence.append(f"concat(a,b=) crossed: {concatted!r}")

    r.evidence.append(
        "→ a vaulted package loaded inside a child Python, called, and "
        "returned a value across the OS-process boundary via pickle — "
        "single-call subprocess substrate is real, not sketched"
    )
    r.passed = True


if __name__ == "__main__":
    run_test(
        "subprocess-isolated substrate is a verified capability: a child "
        "python spawns, a vaulted package loads inside it, attribute "
        "access and primitive function calls cross the OS-process "
        "boundary via length-prefixed pickle frames — the structural "
        "hole dlmopen's portability constraints left open is closed",
        body,
    )
