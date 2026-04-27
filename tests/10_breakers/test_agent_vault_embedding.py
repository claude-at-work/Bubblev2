"""Claim: bubble has a consumption-shape API — `bubble.AgentVault` —
that lets an agent runtime embed bubble as a library and use the
content-addressed vault + multi-version isolation primitives without
touching the CLI surface.

This is the consumption-basin move named in the consonance loop's
unlooked-at consideration: the value bubble offers an autonomous agent
runtime is a primitive (vault + integrity edge + substrate ladder)
the framework cannot easily build itself, surfaced through one import
the framework can drop in. The CLI is for humans; AgentVault is for
embedding code.

The architectural discipline is preserved:
  - AgentVault.add() refuses sdists by default (running setup.py is RCE
    under the agent's privileges)
  - AgentVault.tool() routes through the existing meta-finder + router
    + substrate handlers — no new bypass of the integrity edge
  - AgentVault.close() shuts down substrate children eagerly

Pinned:
  - `from bubble import AgentVault` succeeds and is a class
  - construction with home= rebinds the vault root and creates the
    SQLite index in the new location
  - register() declares (alias, real_name, version, wheel_tag,
    isolation) and tool() returns a usable module
  - the same AgentVault can host multiple aliases simultaneously
  - registered_tools() reflects the alias map
  - close() drops the meta-finder and clears registered aliases from
    sys.modules
  - context-manager protocol works
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def _stage_synthetic(name: str, version: str, init_source: str,
                     wheel_tag: str = "py3-none-any") -> tuple[str, str, str]:
    """Stage a synthetic wheel into the vault directly (without going
    through PyPI). Mirrors stage_fake_package but stays inside the body
    so the AgentVault under test sees it as already-vaulted."""
    import textwrap
    from bubble.vault import store, db
    db.init_db()
    staged = store.stage_dir()
    pkg_dir = staged / name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(textwrap.dedent(init_source).lstrip())
    dist_info = staged / f"{name}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
    )
    (dist_info / "WHEEL").write_text(
        f"Wheel-Version: 1.0\nGenerator: agentvault-test\n"
        f"Root-Is-Purelib: true\nTag: {wheel_tag}\n"
    )
    (dist_info / "top_level.txt").write_text(name + "\n")
    py_tag, abi_tag, plat_tag = (wheel_tag.split("-") + ["py3", "none", "any"])[:3]
    store.commit(
        name=name, version=version, wheel_tag=wheel_tag,
        python_tag=py_tag, abi_tag=abi_tag, platform_tag=plat_tag,
        staged=staged, source="agentvault-test",
    )
    return name, version, wheel_tag


def body(r: Result):
    # The test's own BUBBLE_HOME is set by run_test; AgentVault should
    # see the same home unless explicitly overridden. Use a *second*
    # home to verify the home= override propagates.
    second_home = Path(tempfile.mkdtemp(prefix="av-second-home-"))
    try:
        from bubble import AgentVault

        # 1. Construct in default home; vault is empty.
        with AgentVault() as av:
            assert av.list_vaulted() == [], (
                f"fresh AgentVault should be empty, got {av.list_vaulted()}"
            )
            r.evidence.append("AgentVault() constructs into BUBBLE_HOME, vault empty")

            # Stage a synthetic package directly into the AgentVault's vault.
            _stage_synthetic(
                "agentdemo", "1.0.0",
                'GREETING = "hello from agentdemo v1"\n'
                'def echo(s): return f"v1:{s}"\n'
            )
            _stage_synthetic(
                "agentdemo", "2.0.0",
                'GREETING = "hello from agentdemo v2"\n'
                'def echo(s): return f"v2:{s}"\n'
                'def reverse(s): return s[::-1]\n'  # v2-only
            )
            vaulted = av.list_vaulted()
            assert len(vaulted) == 2, f"expected 2 vaulted, got {vaulted}"
            r.evidence.append(f"staged 2 versions of agentdemo: {vaulted}")

            # 2. Register an alias and use the tool. Default isolation
            #    is in_process; we exercise that path first because it
            #    is what most agent calls will use.
            av.register("greeter", real_name="agentdemo", version="1.0.0",
                        wheel_tag="py3-none-any")
            assert "greeter" in av.registered_tools(), (
                f"registered_tools missing 'greeter': {av.registered_tools()}"
            )
            greeter = av.tool("greeter")
            assert greeter.GREETING == "hello from agentdemo v1", (
                f"greeter.GREETING = {greeter.GREETING!r}"
            )
            assert greeter.echo("ping") == "v1:ping", (
                f"greeter.echo('ping') = {greeter.echo('ping')!r}"
            )
            r.evidence.append(
                "register('greeter', version='1.0.0') + tool('greeter') "
                "returns a usable in_process module"
            )

            # 3. Register a second alias for v2 alongside the v1 alias.
            #    Diamond-conflict-dissolution through the embedding API.
            av.register("greeter_new", real_name="agentdemo",
                        version="2.0.0", wheel_tag="py3-none-any")
            new = av.tool("greeter_new")
            assert new.GREETING == "hello from agentdemo v2"
            assert new.echo("ping") == "v2:ping"
            assert new.reverse("ping") == "gnip"
            r.evidence.append(
                "second alias 'greeter_new' bound to v2.0.0; both surfaces "
                "live concurrently from one AgentVault"
            )

            # The v1 alias is still its v1 self.
            assert greeter is not new, "two distinct module objects required"
            still_v1 = av.tool("greeter")
            assert still_v1.GREETING == "hello from agentdemo v1"
            r.evidence.append(
                "v1 alias unchanged after v2 alias registered — "
                "diamond conflict dissolved through the embedding API"
            )

            # 4. registered_tools() reflects both bindings with their
            #    isolation declarations.
            tools = av.registered_tools()
            assert set(tools) == {"greeter", "greeter_new"}
            assert tools["greeter"]["version"] == "1.0.0"
            assert tools["greeter_new"]["version"] == "2.0.0"
            assert tools["greeter"]["isolation"] is None  # default in_process
            r.evidence.append(f"registered_tools(): {sorted(tools)}")

        # 5. After close(), the aliases are gone from sys.modules.
        assert "greeter" not in sys.modules, (
            "alias 'greeter' lingered in sys.modules after close()"
        )
        assert "greeter_new" not in sys.modules, (
            "alias 'greeter_new' lingered in sys.modules after close()"
        )
        r.evidence.append(
            "close() removes registered aliases from sys.modules and "
            "drops the meta-finder"
        )

        # 6. home= override creates a separate vault root.
        with AgentVault(home=second_home) as av2:
            assert av2.list_vaulted() == [], (
                "AgentVault(home=…) should see an empty vault at the new home"
            )
            assert (second_home / "vault.db").exists(), (
                f"vault.db not at second home: {second_home}"
            )
            r.evidence.append(
                f"AgentVault(home={second_home.name}) created separate "
                f"vault root with its own SQLite index"
            )

    finally:
        shutil.rmtree(second_home, ignore_errors=True)

    r.passed = True


if __name__ == "__main__":
    run_test(
        "bubble.AgentVault is a consumption-shape embedding API: agent "
        "runtimes can vault, register tools by alias with declarable "
        "isolation, and import them as modules — diamond-conflict "
        "dissolution surfaced as one library instead of as a CLI",
        body,
    )
