"""bubble.pyproject: parse pyproject.toml, requirements.txt, setup.cfg."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble.pyproject import parse_deps, find_manifest

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)

        # pyproject.toml — PEP 621 array form
        (d / "pyproject.toml").write_text(
            '[project]\n'
            'name = "myapp"\n'
            'dependencies = [\n'
            '    "requests>=2.0",\n'
            '    "numpy",\n'
            '    "beautifulsoup4",\n'
            ']\n'
        )
        deps = parse_deps(d / "pyproject.toml")
        r.evidence.append(f"pep621 deps: {deps}")
        assert "requests" in deps, "requests missing"
        assert "numpy" in deps, "numpy missing"
        assert "beautifulsoup4" in deps, "beautifulsoup4 missing"

        # requirements.txt
        (d / "sub" / "proj").mkdir(parents=True)
        req = d / "sub" / "proj" / "requirements.txt"
        req.write_text(
            "# comment\n"
            "flask>=2.0\n"
            "sqlalchemy\n"
            "-r other.txt\n"   # skip -r includes
            "click[testing]\n"
        )
        deps2 = parse_deps(req)
        r.evidence.append(f"requirements deps: {deps2}")
        assert "flask" in deps2, "flask missing"
        assert "sqlalchemy" in deps2
        assert "click" in deps2
        assert "-r other.txt" not in deps2

        # setup.cfg
        cfg = d / "setup.cfg"
        cfg.write_text(
            "[options]\n"
            "install_requires =\n"
            "    pydantic>=1.0\n"
            "    httpx\n"
        )
        deps3 = parse_deps(cfg)
        r.evidence.append(f"setup.cfg deps: {deps3}")
        assert "pydantic" in deps3
        assert "httpx" in deps3

        # find_manifest: walk up from a subdir
        found = find_manifest(d / "sub" / "proj")
        r.evidence.append(f"find_manifest from sub/proj: {found}")
        assert found is not None
        assert found.name == "requirements.txt"

    r.passed = True


if __name__ == "__main__":
    run_test("pyproject.parse_deps handles pyproject.toml / requirements.txt / setup.cfg", body)
