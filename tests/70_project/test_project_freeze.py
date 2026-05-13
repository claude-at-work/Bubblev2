"""bubble project freeze: live shell snapshot round-trips as a deployment manifest."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result, stage_fake_package


def body(r: Result):
    from bubble.vault import db
    from bubble.run import shell as shell_mod
    from bubble import project as proj_mod
    from bubble import manifest as manifest_mod

    db.init_db()
    stage_fake_package(name="foxtrot", version="5.0.0", import_name="foxtrot")

    shell_mod.create("freeze-test", [], exist_ok=True)
    shell_mod.add_pinned("freeze-test", "foxtrot", "5.0.0", "py3-none-any")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "freeze-test.toml"
        proj_mod.freeze("freeze-test", out)

        r.evidence.append(f"manifest written: {out.exists()}")
        assert out.exists(), "manifest file not written"

        m = manifest_mod.load(out)
        r.evidence.append(f"packages: {list(m.packages.keys())}")
        assert "foxtrot" in m.packages, "foxtrot not in frozen manifest"
        ver, tag = m.packages["foxtrot"]
        assert ver == "5.0.0", f"wrong version in manifest: {ver}"
        r.evidence.append(f"foxtrot=={ver} @{tag}")

    r.passed = True


if __name__ == "__main__":
    run_test("shell freeze produces a round-trippable deployment manifest", body)
