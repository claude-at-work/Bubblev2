"""Claim: `importlib.metadata` queries from inside an alias resolve against
the alias's vault dist-info — not the host's installed dist-info, and not
some other alias's.

Why this matters: modern packages compute `__version__` (and increasingly
their entry-point graph and feature flags) via `importlib.metadata.version(
__name__)` rather than a hardcoded string. Click 8.3+ does this; the
deprecation warning click prints when you read `click.__version__` literally
tells the caller to switch. If bubble's vault doesn't shim
`importlib.metadata`, two aliases of the same package will both report
whichever version happens to be installed in the host venv — silently
collapsing the diamond-conflict story for any tool that reads its own
metadata.

This is the test that makes that claim load-bearing instead of
"happens to work for hardcoded versions only."

Proof shape:
  1. Stage two synthetic versions of `widget`. Each `__init__.py` runs
     `importlib.metadata.version('widget')` at import time and captures
     the result into a module attribute `VERSION_VIA_METADATA`.
  2. Load both as aliases under different names.
  3. Assert each alias's `VERSION_VIA_METADATA` matches the vault version
     it was loaded from — not the host's view, not the other alias's.

If the test fails before the fix, it'll fail with `PackageNotFoundError`
during the first `import widget_old` (because the vault isn't on sys.path
and no other finder claims it). After the fix, both imports succeed and
each alias reports its own version.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


_INIT_SOURCE = """
    from importlib.metadata import version, metadata
    VERSION_VIA_METADATA = version("widget")
    NAME_VIA_METADATA = metadata("widget")["Name"]
"""


def body(r: Result):
    stage_fake_package(
        name="widget",
        version="1.0.0",
        import_name="widget",
        init_source=_INIT_SOURCE,
    )
    stage_fake_package(
        name="widget",
        version="2.0.0",
        import_name="widget",
        init_source=_INIT_SOURCE,
    )

    aliases = {
        "widget_old": ("widget", "1.0.0", "py3-none-any"),
        "widget_new": ("widget", "2.0.0", "py3-none-any"),
    }

    with vault_finder(aliases=aliases):
        import widget_old  # type: ignore
        import widget_new  # type: ignore

        assert widget_old.VERSION_VIA_METADATA == "1.0.0", (
            f"widget_old reports {widget_old.VERSION_VIA_METADATA!r} via "
            f"importlib.metadata; expected '1.0.0'. The vault's dist-info "
            f"isn't being served per-alias."
        )
        assert widget_new.VERSION_VIA_METADATA == "2.0.0", (
            f"widget_new reports {widget_new.VERSION_VIA_METADATA!r} via "
            f"importlib.metadata; expected '2.0.0'."
        )
        assert widget_old.NAME_VIA_METADATA == "widget"
        assert widget_new.NAME_VIA_METADATA == "widget"

        # And the views are actually distinct — not the same string by
        # accident of the host's installed widget (there isn't one).
        assert widget_old.VERSION_VIA_METADATA != widget_new.VERSION_VIA_METADATA

        r.evidence.append(
            f"widget_old via importlib.metadata: "
            f"version={widget_old.VERSION_VIA_METADATA!r}, "
            f"name={widget_old.NAME_VIA_METADATA!r}"
        )
        r.evidence.append(
            f"widget_new via importlib.metadata: "
            f"version={widget_new.VERSION_VIA_METADATA!r}, "
            f"name={widget_new.NAME_VIA_METADATA!r}"
        )
        r.evidence.append(
            "→ each alias resolves importlib.metadata against its own "
            "vault dist-info; the host's view does not leak across the "
            "alias boundary"
        )
    r.passed = True


if __name__ == "__main__":
    run_test(
        "importlib.metadata queries from inside an alias resolve against "
        "that alias's vault dist-info — not the host's, not a sibling "
        "alias's; the diamond-conflict story holds for metadata-driven "
        "packages, not just hardcoded-__version__ ones",
        body,
    )
