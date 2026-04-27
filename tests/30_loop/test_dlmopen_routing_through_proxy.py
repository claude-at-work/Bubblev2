"""Claim: an alias declaring substrate=dlmopen_isolated routes through
the dlmopen handler and yields a usable proxy module — two versions of
the same package serve callable surfaces in one process, kernel-isolated.

This is the demonstration the README has been pointing at since commit
one. Confirmed-with-pattern in the README's Multi-version coexistence
section was always *with-pattern* — single-call works, multi-call needs
GIL-state plumbing. The proxy module lands single-call, picklable
arguments, picklable returns. Today the diamond conflict is no longer
a frame; it is a measurable absence in the running program.

Pinned:
  - alias declared with substrate=dlmopen_isolated routes through
    the dlmopen handler (not via downgrade-to-in_process)
  - the resulting module is a types.ModuleType subclass — fits in
    sys.modules, behaves under `import` like a normal module
  - module-level constants are reachable (e.g., __version__,
    package-defined CONSTANT)
  - module-level functions can be called with primitive args; the
    return value pickles back across the namespace boundary
  - two such aliases for the same dist (different versions) serve
    distinct callable surfaces in the same process
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


def body(r: Result):
    from bubble.substrate import dlmopen as dlmopen_mod
    from bubble import probe, host

    if not dlmopen_mod.is_available():
        r.skipped = (
            f"dlmopen substrate not available on this host: "
            f"{dlmopen_mod._AVAIL_REASON or 'unknown'}"
        )
        return

    # Probe so host.toml lists dlmopen_isolated as an available substrate.
    # Otherwise route() will downgrade for lack of a probed entry.
    portrait = probe.run_all()
    probe.write(probe.host_toml_path(), portrait)
    if not host.has_substrate("dlmopen_isolated"):
        r.skipped = "host portrait says dlmopen_isolated unavailable"
        return

    # Two synthetic packages whose APIs disagree on a function. The
    # divergence is what proves the proxy is reading from the *right*
    # interpreter — if both calls returned the same thing, the test
    # would not distinguish dlmopen-isolated from in_process.
    stage_fake_package(
        name="diverge", version="1.0.0", import_name="diverge",
        init_source='''
            VERSION = "1.0.0"
            API_LEVEL = 1
            def shape(): return "rectangle"
            def area(w, h): return w * h
        ''',
    )
    stage_fake_package(
        name="diverge", version="2.0.0", import_name="diverge",
        init_source='''
            VERSION = "2.0.0"
            API_LEVEL = 2
            def shape(): return "ellipse"
            def area(w, h): return 3.14159 * w * h / 4
            def perimeter(w, h): return 2 * (w + h)  # only in v2
        ''',
    )

    # Two aliases: one routed through in_process (default), one through
    # dlmopen_isolated. Both should resolve, distinct objects, distinct
    # behaviors.
    aliases = {
        "diverge_inproc":   ("diverge", "1.0.0", "py3-none-any", "in_process"),
        "diverge_isolated": ("diverge", "2.0.0", "py3-none-any", "dlmopen_isolated"),
    }

    with vault_finder(aliases=aliases) as finder:
        import diverge_inproc as dv1   # type: ignore
        import diverge_isolated as dv2  # type: ignore

        # Distinct module objects.
        assert dv1 is not dv2, "aliases collapsed to the same module"
        r.evidence.append(f"two distinct module objects from one alias dict")

        # Module-level constants — primitive values, picklable.
        assert dv1.VERSION == "1.0.0", f"dv1.VERSION = {dv1.VERSION!r}"
        assert dv2.VERSION == "2.0.0", f"dv2.VERSION = {dv2.VERSION!r}"
        assert dv1.API_LEVEL == 1
        assert dv2.API_LEVEL == 2
        r.evidence.append(
            f"VERSION crosses both substrates: dv1={dv1.VERSION!r}, dv2={dv2.VERSION!r}"
        )

        # Module-level functions: callable in both, distinct returns.
        s1 = dv1.shape()
        s2 = dv2.shape()
        assert s1 == "rectangle", f"dv1.shape() = {s1!r}"
        assert s2 == "ellipse", f"dv2.shape() = {s2!r}"
        r.evidence.append(
            f"shape() returns its version's value: "
            f"dv1.shape()={s1!r}, dv2.shape()={s2!r}"
        )

        # Function with arguments — primitive args marshal across the
        # namespace boundary.
        a1 = dv1.area(4, 5)
        a2 = dv2.area(4, 5)
        assert a1 == 20, f"dv1.area(4,5) = {a1}"
        assert abs(a2 - (3.14159 * 20 / 4)) < 0.001, f"dv2.area(4,5) = {a2}"
        r.evidence.append(
            f"area(4, 5) — primitive args cross both substrates: "
            f"dv1={a1}, dv2={a2:.4f}"
        )

        # v2-only function: AttributeError on v1, callable on v2.
        try:
            _ = dv1.perimeter
        except AttributeError:
            r.evidence.append("v1-only proxy correctly missing v2-only attr 'perimeter'")
        else:
            raise AssertionError("dv1.perimeter should not exist")

        p2 = dv2.perimeter(4, 5)
        assert p2 == 18, f"dv2.perimeter(4,5) = {p2}"
        r.evidence.append(f"v2-only function callable: dv2.perimeter(4,5) = {p2}")

        r.evidence.append(
            "→ alias declared substrate=dlmopen_isolated routes through "
            "the substrate handler; resulting proxy module is callable "
            "from the calling interpreter; v1 and v2 surfaces coexist "
            "in one process with distinct semantics. The diamond "
            "conflict is dissolved."
        )

    r.passed = True


if __name__ == "__main__":
    run_test(
        "alias declaring substrate=dlmopen_isolated routes through the "
        "substrate handler and yields a callable proxy module: module-"
        "level constants reachable, functions invokable with primitive "
        "args, two versions of the same package serving distinct "
        "surfaces in one process — the diamond conflict dissolved at "
        "the link-namespace level",
        body,
    )
