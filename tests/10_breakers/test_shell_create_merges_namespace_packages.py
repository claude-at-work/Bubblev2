"""Claim: when N>1 vault packages contribute the same top-level import
name (PEP 420 namespace package), shell creation builds lib/<top>/ as
a real directory of subdir-symlinks rather than one dir-level symlink
that shadows all but one contribution.

Concrete real-world case: opentelemetry is contributed by 7 separate
PyPI packages (api, sdk, proto, semantic-conventions, exporter-otlp-
proto-{common,grpc,http}). Without this, `from opentelemetry import
baggage` raises ImportError because only one contribution's
opentelemetry/ dir is linked.

Sub-case 3 of issue #13.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble.run import shell as shell_mod
    from bubble.vault import db
    db.init_db()

    # Two synthetic packages contribute the same top-level "ns".
    # ns_a contributes ns/alpha, ns_b contributes ns/beta. Both ship with
    # an empty ns/__init__.py that PEP 420 namespace-package semantics
    # would handle, but bubble uses content links, so we test that both
    # subdirs end up reachable.
    stage_fake_package(name="ns-a", version="1.0.0", import_name="ns",
                       submodules={"alpha": 'NAME = "alpha"\n'})
    stage_fake_package(name="ns-b", version="1.0.0", import_name="ns",
                       submodules={"beta": 'NAME = "beta"\n'})

    sd = shell_mod.create("nstest", ["ns-a", "ns-b"])
    r.evidence.append(f"shell at {sd}")

    ns_dir = sd / "lib" / "ns"
    if ns_dir.is_symlink():
        target = (sd / "lib").joinpath(*ns_dir.readlink().parts)
        r.error = (
            f"lib/ns is a single dir-level symlink to {ns_dir.readlink()} — "
            f"the namespace-merge path was not taken; one contribution shadows "
            f"the other."
        )
        return
    if not ns_dir.is_dir():
        r.error = f"lib/ns is not a directory: {ns_dir}"
        return

    have = sorted(p.name for p in ns_dir.iterdir())
    r.evidence.append(f"lib/ns/ contents: {have}")

    # alpha.py from ns-a and beta.py from ns-b should both be present.
    for required in ("alpha.py", "beta.py"):
        if required not in have:
            r.error = (
                f"namespace merge missing {required}; lib/ns/ has {have}. "
                f"Both contributions must be reachable inside the merged dir."
            )
            return

    # Verify both can be imported when lib/ is on sys.path.
    import subprocess
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); "
         "from ns import alpha, beta; "
         "print(alpha.NAME, beta.NAME)" % str(sd / "lib")],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        r.error = f"import test failed: {result.stderr}"
        return
    if "alpha beta" not in result.stdout:
        r.error = f"import returned wrong values: {result.stdout!r}"
        return

    r.evidence.append(f"both contributions importable: {result.stdout.strip()}")

    # Recursive merge — the deeper case real namespace packages exhibit.
    # opentelemetry-exporter-otlp-proto-{common,grpc,http} all ship
    # opentelemetry/exporter/otlp/proto/{common,grpc,http} respectively,
    # so the merge has to recurse where contributions agree on intermediate
    # directories. Without recursion, lib/ns/exporter/ would be a single
    # symlink to one contribution, shadowing the others' nested subtrees.
    stage_fake_package(
        name="ns-c", version="1.0.0", import_name="ns",
        submodules={"shared": 'NAME = "from_c"\n'},
    )
    stage_fake_package(
        name="ns-d", version="1.0.0", import_name="ns",
        submodules={"shared": 'NAME = "from_d"\n'},
    )
    # Recreate so we get a fresh closure that includes ns-c and ns-d.
    shell_mod.delete("nstest")
    sd = shell_mod.create("nstest", ["ns-a", "ns-b", "ns-c", "ns-d"])
    ns_dir = sd / "lib" / "ns"
    have = sorted(p.name for p in ns_dir.iterdir())
    if "alpha.py" not in have or "beta.py" not in have:
        r.error = f"after recreate with 4 contributions, lib/ns/ missing original entries: {have}"
        return
    r.evidence.append(f"4-way merge stable: lib/ns/ = {have}")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "shell creation merges namespace-package contributions: when N>1 "
        "vault packages claim the same top-level import name, lib/<top>/ "
        "becomes a real directory of subdir-symlinks rather than a single "
        "dir-symlink that shadows all but one. Closes the diamond-conflict "
        "story for namespace-distributed packages like opentelemetry",
        body,
    )
