"""Claim: bridge command routes to the right runtime and applies hardening defaults."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _common import run_test, Result


def body(r: Result):
    from bubble import bridge

    py = Path("/tmp/bridge_route_test.py")
    py.write_text("print('ok')\n")
    js = Path("/tmp/bridge_route_test.js")
    js.write_text("console.log('ok')\n")

    py_args = SimpleNamespace(
        script=str(py),
        args=["--hello"],
        fetch=False,
        no_isolate=False,
        allow_legacy_network=False,
        keep=False,
        dry_run=True,
    )
    rc = bridge.run(py_args)
    assert rc == 0, f"python dry-run should pass, rc={rc}"

    cmd_py = bridge._python_cmd(py.resolve(), ["--hello"], fetch=False, isolate=True)
    assert "run" in cmd_py and "--isolate" in cmd_py
    r.evidence.append(".py routes to main bubble run with isolation by default")

    js_args = SimpleNamespace(
        script=str(js),
        args=[],
        fetch=False,
        no_isolate=False,
        allow_legacy_network=False,
        keep=False,
        dry_run=True,
    )
    rc = bridge.run(js_args)
    assert rc == 2, "legacy path requires explicit network authorization"
    r.evidence.append("legacy route is fail-closed unless --allow-legacy-network is explicit")

    env = bridge._hardened_env({"PATH": "/bin", "HOME": "/home/u"})
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONSAFEPATH"] == "1"
    assert "PYTHONPATH" not in env
    r.evidence.append("bridge runs with reduced, hardened environment")

    py.unlink(missing_ok=True)
    js.unlink(missing_ok=True)
    r.passed = True


if __name__ == "__main__":
    run_test(
        "bridge orchestrates main + legacy runtimes while preserving strict defaults and hardening",
        body,
    )
