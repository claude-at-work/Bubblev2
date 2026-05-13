"""Fault loop: pyproject dist name lookup supplements IMPORT_TO_DIST table."""
import sys
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result, stage_fake_package


def body(r: Result):
    """_dist_from_pyproject resolves import names that are not in
    IMPORT_TO_DIST but ARE declared in pyproject.toml.

    Example: `import myspecialpkg` where IMPORT_TO_DIST has no entry but
    pyproject.toml lists `myspecialpkg` as a dependency — the normalised
    name match confirms the dist name.
    """
    from bubble.vault import db
    from bubble.meta_finder import VaultFinder

    db.init_db()

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "pyproject.toml").write_text(
            '[project]\n'
            'name = "demo"\n'
            'dependencies = ["MySpecialPkg", "another-lib"]\n'
        )

        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            finder = VaultFinder(autofetch=True)
            # Force load of pyproject dists without triggering prefetch
            dists = finder._load_pyproject_dists()
            r.evidence.append(f"parsed dists: {dists}")

            # myspecialpkg normalises to same key as MySpecialPkg
            resolved = finder._dist_from_pyproject("myspecialpkg")
            r.evidence.append(f"myspecialpkg → {resolved}")
            assert resolved == "MySpecialPkg", (
                f"expected MySpecialPkg, got {resolved}"
            )

            # another_lib (underscore) should match another-lib (hyphen)
            resolved2 = finder._dist_from_pyproject("another_lib")
            r.evidence.append(f"another_lib → {resolved2}")
            assert resolved2 == "another-lib", (
                f"expected another-lib, got {resolved2}"
            )

            # unknown name: pass-through
            resolved3 = finder._dist_from_pyproject("not_in_pyproject")
            r.evidence.append(f"not_in_pyproject → {resolved3}")
            assert resolved3 == "not_in_pyproject", "unknown name should pass through"
        finally:
            os.chdir(old_cwd)

    r.passed = True


if __name__ == "__main__":
    run_test("_dist_from_pyproject resolves undeclared import names via pyproject.toml", body)
