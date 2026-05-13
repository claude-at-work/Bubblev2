"""bubble project ingest: scope is saved to shell metadata."""
import sys
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result, stage_fake_package


def body(r: Result):
    from bubble.vault import db
    from bubble import project as proj_mod
    from bubble.run.shell import load_scope

    db.init_db()
    stage_fake_package(name="bravo", version="2.0.0", import_name="bravo")

    with tempfile.TemporaryDirectory() as tmp:
        proj_dir = Path(tmp) / "proj"
        proj_dir.mkdir()
        (proj_dir / "app.py").write_text("import bravo\n")

        proj_mod.ingest(proj_dir, "test-scope", overwrite=True)

        scope = load_scope("test-scope")
        r.evidence.append(f"scope: {scope}")

        assert scope is not None, "scope not saved to shell metadata"
        assert "bravo" in scope, "bravo not in scope"
        ver, tag = scope["bravo"]
        assert ver == "2.0.0", f"unexpected version in scope: {ver}"

    r.passed = True


if __name__ == "__main__":
    run_test("ingest persists package scope to shell metadata", body)
