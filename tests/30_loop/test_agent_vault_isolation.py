"""Claim: AgentVault's embedding API exposes the substrate ladder
declaratively — an agent runtime can register two tools backed by the
same dist with different isolation rings, and the diamond conflict
dissolves through whichever substrate the host can serve.

This is the substrate-aware path of the consumption-shape move:
register('a', isolation='in_process') and register('b',
isolation='subprocess') and watch the two coexist in one process tree
with different runtime shapes. The dlmopen test
(test_dlmopen_routing_through_proxy.py) and the subprocess test
(test_subprocess_routing_through_proxy.py) prove the substrate
mechanics from the meta-finder side; this test proves the same
mechanics are reachable through the agent-facing surface.

Pinned:
  - register(isolation='subprocess') routes the alias through the
    subprocess substrate handler
  - register(isolation='in_process') routes the alias through the
    direct-link path
  - both aliases for the same dist coexist in one caller-process tree
  - the in_process tool is a real module; the subprocess tool is an
    IsolatedModule (proxy)
  - close() cleans up the substrate child interpreter
"""
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def _stage(name, version, init_source, wheel_tag="py3-none-any"):
    from bubble.vault import store, db
    db.init_db()
    staged = store.stage_dir()
    pkg_dir = staged / name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(textwrap.dedent(init_source).lstrip())
    di = staged / f"{name}-{version}.dist-info"
    di.mkdir()
    (di / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
    )
    (di / "WHEEL").write_text(
        f"Wheel-Version: 1.0\nGenerator: avi-test\n"
        f"Root-Is-Purelib: true\nTag: {wheel_tag}\n"
    )
    (di / "top_level.txt").write_text(name + "\n")
    py, abi, plat = (wheel_tag.split("-") + ["py3", "none", "any"])[:3]
    store.commit(
        name=name, version=version, wheel_tag=wheel_tag,
        python_tag=py, abi_tag=abi, platform_tag=plat,
        staged=staged, source="agentvault-isolation-test",
    )


def body(r: Result):
    from bubble import AgentVault
    from bubble.substrate import subprocess as sub_mod
    from bubble import probe, host

    if not sub_mod.is_available():
        r.skipped = (
            f"subprocess substrate not available: "
            f"{sub_mod._AVAIL_REASON or 'unknown'}"
        )
        return
    portrait = probe.run_all()
    probe.write(probe.host_toml_path(), portrait)
    if not host.has_substrate("subprocess"):
        r.skipped = "host portrait says subprocess unavailable"
        return

    _stage("avi", "1.0.0",
           'VERSION = "1.0.0"\n'
           'def label(): return "v1-inproc"\n'
           'def square(x): return x * x\n')
    _stage("avi", "2.0.0",
           'VERSION = "2.0.0"\n'
           'def label(): return "v2-subproc"\n'
           'def cube(x): return x * x * x\n')
    r.evidence.append("staged avi 1.0.0 + 2.0.0 in vault")

    with AgentVault() as av:
        # Two aliases, same dist, different versions, different
        # isolation rings — declared through the agent-facing API only.
        av.register("avi_local", real_name="avi", version="1.0.0",
                    wheel_tag="py3-none-any", isolation="in_process")
        av.register("avi_isolated", real_name="avi", version="2.0.0",
                    wheel_tag="py3-none-any", isolation="subprocess")

        local = av.tool("avi_local")
        isolated = av.tool("avi_isolated")

        assert local is not isolated, "aliases collapsed"
        r.evidence.append("two distinct module objects from registered tools")

        # in_process tool: ordinary module attribute access.
        assert local.VERSION == "1.0.0", f"local.VERSION = {local.VERSION!r}"
        assert local.label() == "v1-inproc"
        assert local.square(7) == 49
        r.evidence.append(
            f"in_process tool: VERSION={local.VERSION!r}, "
            f"label()={local.label()!r}, square(7)={local.square(7)}"
        )

        # subprocess tool: attribute access marshals through the pickle
        # channel into a child python.
        assert isolated.VERSION == "2.0.0", (
            f"isolated.VERSION = {isolated.VERSION!r}"
        )
        assert isolated.label() == "v2-subproc"
        assert isolated.cube(4) == 64
        r.evidence.append(
            f"subprocess tool: VERSION={isolated.VERSION!r}, "
            f"label()={isolated.label()!r}, cube(4)={isolated.cube(4)}"
        )

        # The proxy module's class differs from a normal module — it's
        # a types.ModuleType subclass from the subprocess handler.
        from bubble.substrate.subprocess import IsolatedModule
        assert isinstance(isolated, IsolatedModule), (
            f"expected IsolatedModule, got {type(isolated).__name__}"
        )
        r.evidence.append(
            f"isolated tool is {type(isolated).__name__} from "
            f"bubble.substrate.subprocess"
        )

        # The v2-only function is reachable on the isolated proxy and
        # absent on the in_process module — substrate-correct routing.
        try:
            _ = local.cube
        except AttributeError:
            r.evidence.append(
                "v1 local tool correctly missing v2-only attr 'cube'"
            )
        else:
            raise AssertionError("local.cube should not exist")

        r.evidence.append(
            "→ AgentVault.register(isolation='subprocess') routes through "
            "the subprocess substrate handler; the agent framework calls "
            "tool(alias) and gets a callable proxy. The substrate ladder "
            "is reachable as a declarative property of registration."
        )

    # close() should have shut down the subprocess child.
    from bubble.substrate.subprocess import _INTERP_REGISTRY
    assert "avi_isolated" not in _INTERP_REGISTRY, (
        "subprocess child interpreter survived close()"
    )
    r.evidence.append("close() drained the subprocess interp registry")

    r.passed = True


if __name__ == "__main__":
    run_test(
        "AgentVault.register(isolation='subprocess') drives the subprocess "
        "substrate from the embedding API: an agent declares the isolation "
        "ring per tool, the substrate ladder dispatches accordingly, and "
        "two versions of one dist coexist as differently-shaped tools — "
        "the consumption surface for diamond-conflict dissolution",
        body,
    )
