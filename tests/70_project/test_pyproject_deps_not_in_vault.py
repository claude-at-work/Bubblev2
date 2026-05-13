"""bubble.pyproject.deps_not_in_vault returns only the missing distributions."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result, stage_fake_package


def body(r: Result):
    from bubble.vault import db
    from bubble.pyproject import deps_not_in_vault

    db.init_db()
    # Stage one dep in the vault
    stage_fake_package(name="present-pkg", version="1.0.0", import_name="present_pkg")

    with tempfile.TemporaryDirectory() as tmp:
        req = Path(tmp) / "requirements.txt"
        req.write_text(
            "present-pkg\n"
            "absent-pkg\n"
        )
        missing = deps_not_in_vault(req)
        r.evidence.append(f"missing: {missing}")
        assert "absent-pkg" in missing, "absent-pkg should be missing"
        assert "present-pkg" not in missing, "present-pkg is in vault and should not appear"

    r.passed = True


if __name__ == "__main__":
    run_test("deps_not_in_vault returns only distributions absent from the vault", body)
