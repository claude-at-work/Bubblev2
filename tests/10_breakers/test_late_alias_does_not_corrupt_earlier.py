"""Claim: importing a second version of a package mid-process does not
retroactively change the behavior or identity of the already-imported one.

This is vector 4 from the architecture review — the time axis. The README
talks about spatial coexistence (two aliases at once); this test asks about
*temporal* coexistence: can the dependency graph grow a third axis after
work has already happened against the first?

Conventional intuition: imports are global state. Once `requests` means 2.31,
loading 2.33 risks contaminating earlier imports — through sys.modules,
through transitive imports, through registered entry points. So the safe move
is to never let it happen.

Proof shape:
  1. Import widget (default) under one alias. Capture object identity, version,
     a class reference, an instance.
  2. THEN, with everything from step 1 still alive, import the other version
     under a different alias. Use it, exercise it.
  3. Check that everything captured in step 1 is unchanged: same id, same
     version string, same class reference, instance still behaves like v1.

If that holds, Bubble is doing temporal isolation, not just spatial.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


def body(r: Result):
    stage_fake_package(
        name="widget",
        version="1.0.0",
        import_name="widget",
        init_source='''
            VERSION = "1.0.0"
            STATE = {"calls": 0}
            class Widget:
                def hello(self):
                    STATE["calls"] += 1
                    return "v1 says hi"
        ''',
    )
    stage_fake_package(
        name="widget",
        version="2.0.0",
        import_name="widget",
        init_source='''
            VERSION = "2.0.0"
            STATE = {"calls": 0}
            class Widget:
                def hello(self):
                    STATE["calls"] += 1
                    return "v2 says hi"
        ''',
    )

    aliases = {
        "widget_old": ("widget", "1.0.0", "py3-none-any"),
        "widget_new": ("widget", "2.0.0", "py3-none-any"),
    }

    with vault_finder(aliases=aliases):
        # ── t0: import old, exercise it ──────────────────────────
        import widget_old  # type: ignore
        snap_id_module    = id(widget_old)
        snap_id_class     = id(widget_old.Widget)
        snap_version      = widget_old.VERSION
        snap_instance     = widget_old.Widget()
        snap_hello_t0     = snap_instance.hello()
        snap_calls_t0     = widget_old.STATE["calls"]

        # ── t1: import new, exercise it ──────────────────────────
        import widget_new  # type: ignore
        new_instance      = widget_new.Widget()
        new_hello         = new_instance.hello()
        new_version       = widget_new.VERSION

        # ── t2: re-exercise the old one ───────────────────────────
        re_hello          = snap_instance.hello()
        re_version        = widget_old.VERSION
        re_id_module      = id(widget_old)
        re_id_class       = id(widget_old.Widget)
        re_calls          = widget_old.STATE["calls"]

        # Identity preserved: same module object, same class object
        assert re_id_module == snap_id_module, "module id changed"
        assert re_id_class == snap_id_class, "class id changed"
        assert re_version == snap_version == "1.0.0", "version label drifted"
        assert snap_hello_t0 == "v1 says hi"
        assert new_hello == "v2 says hi"
        assert re_hello == "v1 says hi", "old instance now responds like v2"

        # State accounting: old.STATE was incremented twice (t0, t2),
        # new.STATE was incremented once (t1). They are not the same dict.
        assert re_calls == 2, f"old.STATE.calls expected 2, got {re_calls}"
        assert widget_new.STATE["calls"] == 1, \
            f"new.STATE.calls expected 1, got {widget_new.STATE['calls']}"
        assert widget_old.STATE is not widget_new.STATE, \
            "STATE dicts merged — temporal isolation broke"

        r.evidence.append(f"t0: widget_old.VERSION={snap_version}, "
                          f"hello={snap_hello_t0!r}, calls={snap_calls_t0}")
        r.evidence.append(f"t1: widget_new arrives. VERSION={new_version}, "
                          f"hello={new_hello!r}")
        r.evidence.append(f"t2: re-using widget_old. VERSION={re_version}, "
                          f"hello={re_hello!r}, calls={re_calls}")
        r.evidence.append(f"module id stable:  {snap_id_module:#x} → {re_id_module:#x}")
        r.evidence.append(f"class id stable:   {snap_id_class:#x} → {re_id_class:#x}")
        r.evidence.append(f"STATE dicts distinct: old@{id(widget_old.STATE):#x} "
                          f"vs new@{id(widget_new.STATE):#x}")
        r.evidence.append("→ time axis: a late alias did not contaminate earlier state")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "a late-arriving alias does not retroactively corrupt earlier imports — "
        "Bubble's isolation is temporal, not just spatial",
        body,
    )
