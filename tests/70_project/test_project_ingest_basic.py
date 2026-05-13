"""bubble project ingest: creates shell and .bubble-shell marker."""
import sys
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result, stage_fake_package


def body(r: Result):
    from bubble.vault import db
    from bubble import project as proj_mod

    db.init_db()
    stage_fake_package(name="alpha", version="1.0.0", import_name="alpha")

    with tempfile.TemporaryDirectory() as tmp:
        proj_dir = Path(tmp) / "myproject"
        proj_dir.mkdir()
        (proj_dir / "__init__.py").write_text("")
        (proj_dir / "main.py").write_text("import alpha\n")

        summary = proj_mod.ingest(proj_dir, "test-basic", overwrite=True)

        r.evidence.append(f"scanned_files: {summary['scanned_files']}")
        r.evidence.append(f"resolved: {list(summary['resolved'].keys())}")
        r.evidence.append(f"linked: {summary['linked']}")

        assert summary["scanned_files"] >= 1, "no .py files scanned"
        assert "alpha" in summary["resolved"], "alpha not resolved"
        assert len(summary["linked"]) >= 1, "nothing linked"
        assert summary["shell_dir"].exists(), "shell dir not created"

        marker = proj_dir / ".bubble-shell"
        assert marker.exists(), ".bubble-shell marker not written"
        assert "test-basic" in marker.read_text(), "marker has wrong shell name"

    r.passed = True


if __name__ == "__main__":
    run_test("project ingest creates shell and .bubble-shell marker", body)
