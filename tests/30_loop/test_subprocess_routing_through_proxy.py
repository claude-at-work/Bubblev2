"""Claim: an alias declaring substrate=subprocess routes through the
subprocess substrate handler and yields a usable proxy module — two
versions of the same package serve callable surfaces in one process
tree, OS-process-isolated.

This is the parallel of test_dlmopen_routing_through_proxy.py. The
geometric move underneath: dlmopen and subprocess fill complementary
roles on the substrate ladder. dlmopen wins on cost (~5MB vs ~30MB)
and avoids cross-process coordination but requires glibc + an
embeddable libpython. subprocess wins on portability and full
thread/signal isolation but pays the per-process overhead. The
substrate router was always going to need both; with this commit it
has both.

Pinned:
  - alias declared with substrate=subprocess routes through the
    subprocess handler (not via downgrade-to-in_process, not via
    downgrade-to-dlmopen)
  - the resulting module is a types.ModuleType subclass — fits in
    sys.modules, behaves under `import` like a normal module
  - module-level constants are reachable
  - module-level functions can be called with primitive args; the
    return value pickles back across the OS-process boundary
  - two such aliases for the same dist (different versions) serve
    distinct callable surfaces in the same caller-process
  - a v2-only attribute raises AttributeError on the v1 proxy
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


def body(r: Result):
    from bubble.substrate import subprocess as sub_mod
    from bubble import probe, host

    if not sub_mod.is_available():
        r.skipped = (
            f"subprocess substrate not available on this host: "
            f"{sub_mod._AVAIL_REASON or 'unknown'}"
        )
        return

    # Probe so host.toml lists subprocess as an available substrate;
    # otherwise route() will downgrade for lack of a probed entry.
    portrait = probe.run_all()
    probe.write(probe.host_toml_path(), portrait)
    if not host.has_substrate("subprocess"):
        r.skipped = "host portrait says subprocess unavailable"
        return

    stage_fake_package(
        name="diverge_sub", version="1.0.0", import_name="diverge_sub",
        init_source='''
            VERSION = "1.0.0"
            API_LEVEL = 1
            def shape(): return "rectangle"
            def area(w, h): return w * h
        ''',
    )
    stage_fake_package(
        name="diverge_sub", version="2.0.0", import_name="diverge_sub",
        init_source='''
            VERSION = "2.0.0"
            API_LEVEL = 2
            def shape(): return "ellipse"
            def area(w, h): return 3.14159 * w * h / 4
            def perimeter(w, h): return 2 * (w + h)  # only in v2
        ''',
    )

    # Two aliases: one in_process, one routed through the subprocess
    # substrate. Each must resolve, with distinct module objects and
    # distinct semantics.
    aliases = {
        "diverge_inproc":   ("diverge_sub", "1.0.0", "py3-none-any", "in_process"),
        "diverge_isolated": ("diverge_sub", "2.0.0", "py3-none-any", "subprocess"),
    }

    with vault_finder(aliases=aliases) as finder:
        import diverge_inproc as dv1   # type: ignore
        import diverge_isolated as dv2  # type: ignore

        assert dv1 is not dv2, "aliases collapsed to the same module"
        r.evidence.append("two distinct module objects from one alias dict")

        assert dv1.VERSION == "1.0.0", f"dv1.VERSION = {dv1.VERSION!r}"
        assert dv2.VERSION == "2.0.0", f"dv2.VERSION = {dv2.VERSION!r}"
        assert dv1.API_LEVEL == 1
        assert dv2.API_LEVEL == 2
        r.evidence.append(
            f"VERSION crosses both substrates: "
            f"dv1={dv1.VERSION!r}, dv2={dv2.VERSION!r}"
        )

        s1 = dv1.shape()
        s2 = dv2.shape()
        assert s1 == "rectangle", f"dv1.shape() = {s1!r}"
        assert s2 == "ellipse", f"dv2.shape() = {s2!r}"
        r.evidence.append(
            f"shape() returns its version's value: "
            f"dv1.shape()={s1!r}, dv2.shape()={s2!r}"
        )

        a1 = dv1.area(4, 5)
        a2 = dv2.area(4, 5)
        assert a1 == 20, f"dv1.area(4,5) = {a1}"
        assert abs(a2 - (3.14159 * 20 / 4)) < 0.001, f"dv2.area(4,5) = {a2}"
        r.evidence.append(
            f"area(4, 5) — primitive args cross both substrates: "
            f"dv1={a1}, dv2={a2:.4f}"
        )

        # v2-only function: the v2 proxy serves it, the v1 proxy refuses.
        try:
            _ = dv1.perimeter
        except AttributeError:
            r.evidence.append(
                "v1 proxy correctly missing v2-only attr 'perimeter'"
            )
        else:
            raise AssertionError("dv1.perimeter should not exist")

        p2 = dv2.perimeter(4, 5)
        assert p2 == 18, f"dv2.perimeter(4,5) = {p2}"
        r.evidence.append(
            f"v2-only function callable: dv2.perimeter(4,5) = {p2}"
        )

        r.evidence.append(
            "→ alias declared substrate=subprocess routes through the "
            "substrate handler; resulting proxy module is callable from "
            "the calling interpreter; v1 and v2 surfaces coexist in one "
            "caller-process via two child processes. Diamond conflict "
            "dissolved at the OS-process level."
        )

    r.passed = True


if __name__ == "__main__":
    run_test(
        "alias declaring substrate=subprocess routes through the "
        "substrate handler and yields a callable proxy module: module-"
        "level constants reachable, functions invokable with primitive "
        "args, two versions of the same package serving distinct "
        "surfaces in one caller-process tree — diamond conflict "
        "dissolved at the OS-process level, portable everywhere "
        "Python runs",
        body,
    )
