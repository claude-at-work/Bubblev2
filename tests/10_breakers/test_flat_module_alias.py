"""Claim: aliases work for flat single-file modules, not just packages.

Conventional intuition: 'two versions of the same package coexist in one
process via aliases' shouldn't quietly turn into 'two versions of the same
PACKAGE-DIRECTORY-LAYOUT distribution coexist'. `six` is the canonical
flat module — it ships as `six.py` at the dist root, not `six/__init__.py`.

For most of the project's life, `_spec_for_alias` only looked for
`<vault>/<real_name>/__init__.py` and silently returned None for flat
modules. Aliasing `six` looked correct (no error) but yielded
ModuleNotFoundError on import. This test pins down both layouts.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


def body(r: Result):
    stage_fake_package(
        name="flatmod",
        version="1.0.0",
        import_name="flatmod",
        init_source='VERSION = "1.0.0"\ndef where(): return "v1"\n',
        flat=True,
    )
    stage_fake_package(
        name="flatmod",
        version="2.0.0",
        import_name="flatmod",
        init_source='VERSION = "2.0.0"\ndef where(): return "v2"\nNEW = True\n',
        flat=True,
    )

    aliases = {
        "flatmod_old": ("flatmod", "1.0.0", "py3-none-any"),
        "flatmod_new": ("flatmod", "2.0.0", "py3-none-any"),
    }

    with vault_finder(aliases=aliases):
        import flatmod_old  # type: ignore
        import flatmod_new  # type: ignore

        assert flatmod_old.VERSION == "1.0.0"
        assert flatmod_new.VERSION == "2.0.0"
        assert flatmod_old.where() == "v1"
        assert flatmod_new.where() == "v2"
        assert not hasattr(flatmod_old, "NEW")
        assert flatmod_new.NEW is True

        # Module objects are distinct — same flat-file layout, different bytes.
        assert flatmod_old is not flatmod_new
        # __file__ resolves to the actual file in each version's vault tree.
        assert flatmod_old.__file__ != flatmod_new.__file__
        assert flatmod_old.__file__.endswith("/flatmod.py")

        r.evidence.append(f"flatmod_old.__file__: {flatmod_old.__file__}")
        r.evidence.append(f"flatmod_new.__file__: {flatmod_new.__file__}")
        r.evidence.append(f"flatmod_old.where(): {flatmod_old.where()!r}")
        r.evidence.append(f"flatmod_new.where(): {flatmod_new.where()!r}")
        r.evidence.append("→ flat single-file dists alias as cleanly as packages")

    r.passed = True


if __name__ == "__main__":
    run_test(
        "aliases resolve flat single-file modules (e.g. six.py), not only "
        "package-directory layouts — two versions of a flat dist coexist as "
        "distinct module objects in one process",
        body,
    )
