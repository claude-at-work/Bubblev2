"""Claim: two versions of the same package coexist in one process via aliases.

Conventional intuition: `import requests` resolves to one and only one
`requests`. If your dependencies disagree on a version, you split into venvs
or processes. Bubble's alias mechanism says: two aliases, two distinct module
objects, same process, no conflict.

Proof shape: stage two versions of a fake package. Set up aliases. Import
both under their alias names. Check that the module objects are distinct,
their behavior reflects each version's source, and isinstance is asymmetric.
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
            class Widget:
                def hello(self):
                    return "I am widget v1"
        ''',
    )
    stage_fake_package(
        name="widget",
        version="2.0.0",
        import_name="widget",
        init_source='''
            VERSION = "2.0.0"
            class Widget:
                def hello(self):
                    return "I am widget v2"
                def only_in_v2(self):
                    return True
        ''',
    )

    aliases = {
        "widget_old": ("widget", "1.0.0", "py3-none-any"),
        "widget_new": ("widget", "2.0.0", "py3-none-any"),
    }

    with vault_finder(aliases=aliases):
        import widget_old  # type: ignore
        import widget_new  # type: ignore

        assert widget_old.VERSION == "1.0.0"
        assert widget_new.VERSION == "2.0.0"

        w_old = widget_old.Widget()
        w_new = widget_new.Widget()

        assert w_old.hello() == "I am widget v1"
        assert w_new.hello() == "I am widget v2"
        assert w_new.only_in_v2() is True
        assert not hasattr(widget_old.Widget, "only_in_v2"), \
            "v1 should not have v2-only method"

        # Distinct classes — isinstance should be asymmetric
        assert not isinstance(w_old, widget_new.Widget), \
            "v1 instance should not satisfy v2 isinstance"
        assert not isinstance(w_new, widget_old.Widget), \
            "v2 instance should not satisfy v1 isinstance"

        r.evidence.append(f"widget_old.Widget: id={id(widget_old.Widget):#x}")
        r.evidence.append(f"widget_new.Widget: id={id(widget_new.Widget):#x}")
        r.evidence.append(f"widget_old hello:  {w_old.hello()!r}")
        r.evidence.append(f"widget_new hello:  {w_new.hello()!r}")
        r.evidence.append(
            f"isinstance asymmetric: "
            f"v1∈v2={isinstance(w_old, widget_new.Widget)}, "
            f"v2∈v1={isinstance(w_new, widget_old.Widget)}"
        )
        r.evidence.append("→ two versions, one process, distinct classes")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "two versions of the same package coexist in one process via aliases, "
        "with distinct classes and asymmetric isinstance",
        body,
    )
