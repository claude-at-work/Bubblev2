"""Claim: bubble can produce its own deployment artifact (bubble.pyz)
through its own runtime — `python3 -m bubble run tools/build_pyz.py`
walks the recursive self-host one notch — and the build is
deterministic, so the artifact's fingerprint is a function of source
bytes alone.

This is Path B of the bootstrap thread, the safe-side first move:
the build is a pure copy-and-zip operation over bubble's own source
tree, with no third-party dependency to install and therefore no
installation script to run. The discipline the architecture is shaped
around — bubble does not run the installation script — is not tested
here; it is demonstrated still standing.

Determinism is a smaller geodesic move taken alongside: the integrity
edge bubble draws around vault entries (sha256 over bytes, recorded
once at commit) only carries weight if the same source produces the
same bytes. Without determinism, the produced sha256 is a fact about
the run, not about the source. With it, two operators on different
machines building from the same git ref get identical fingerprints —
the deployment artifact becomes content-addressed in the same shape
the vault's entries already are.

Pinned:
  - tools/build_pyz.py exists and is runnable via `python3 -m bubble run`
  - the produced .pyz is a valid zip-app: invoking it as `python3 X.pyz
    --help` returns argparse output naming the bubble subcommands
  - the produced .pyz carries a .sha256 sidecar matching its bytes
  - bubble.pyz can be re-invoked recursively: the produced .pyz is itself
    able to run `vault list` against a fresh BUBBLE_HOME
  - two consecutive builds from the same source produce byte-identical
    archives — the integrity edge bubble draws around vault entries
    extends to the artifact bubble itself ships as
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "tools" / "build_pyz.py"


def body(r: Result):
    if not BUILD_SCRIPT.exists():
        r.passed = False
        r.error = f"build script missing: {BUILD_SCRIPT}"
        return
    r.evidence.append(f"build script: {BUILD_SCRIPT.relative_to(REPO_ROOT)}")

    # Stage a temp output path so we don't write into the repo from a test.
    with tempfile.TemporaryDirectory(prefix="bubble-bootstrap-test-") as td:
        out_pyz = Path(td) / "bubble.pyz"

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        # Drive the build via `bubble run` — recursive self-host: bubble
        # the runtime invokes the build that produces bubble the artifact.
        proc = subprocess.run(
            [sys.executable, "-m", "bubble", "run", str(BUILD_SCRIPT),
             "--", "-o", str(out_pyz)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            r.passed = False
            r.error = (
                f"`bubble run tools/build_pyz.py` failed (rc={proc.returncode})\n"
                f"--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}"
            )
            return
        r.evidence.append("bubble run tools/build_pyz.py: rc=0")

        if not out_pyz.exists():
            r.passed = False
            r.error = f"build script reported success but {out_pyz} missing"
            return
        size = out_pyz.stat().st_size
        r.evidence.append(f"produced artifact: {size} bytes")
        assert size > 1000, "pyz suspiciously small"

        # The sidecar hash must match the bytes on disk.
        sidecar = out_pyz.with_suffix(out_pyz.suffix + ".sha256")
        assert sidecar.exists(), f"sidecar missing: {sidecar}"
        recorded = sidecar.read_text().split()[0]
        h = hashlib.sha256()
        with out_pyz.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        actual = h.hexdigest()
        assert recorded == actual, (
            f"sidecar hash {recorded} != actual {actual}"
        )
        r.evidence.append(f"sidecar sha256 matches bytes: {actual[:16]}…")

        # Smoke: the produced pyz responds to --help and names bubble's
        # subcommands. Anything load-bearing breaks here.
        smoke = subprocess.run(
            [sys.executable, str(out_pyz), "--help"],
            capture_output=True, text=True, timeout=20,
        )
        assert smoke.returncode == 0, (
            f"produced pyz --help failed (rc={smoke.returncode}): "
            f"{smoke.stderr}"
        )
        for token in ("vault", "shell", "run", "probe", "host", "bridge"):
            assert token in smoke.stdout, (
                f"produced pyz --help missing subcommand {token!r}\n"
                f"stdout was:\n{smoke.stdout}"
            )
        r.evidence.append(
            "produced pyz --help responds and lists bubble subcommands"
        )

        # Recursive smoke: the produced .pyz can run itself again, e.g.
        # `produced.pyz vault list` against an empty vault returns cleanly.
        with tempfile.TemporaryDirectory(prefix="bubble-bootstrap-vault-") as vd:
            sub_env = dict(env)
            sub_env["BUBBLE_HOME"] = vd
            recursed = subprocess.run(
                [sys.executable, str(out_pyz), "vault", "list"],
                env=sub_env, capture_output=True, text=True, timeout=20,
            )
            assert recursed.returncode == 0, (
                f"produced pyz `vault list` failed: {recursed.stderr}"
            )
            r.evidence.append(
                f"produced pyz `vault list` returned: "
                f"{recursed.stdout.strip()!r}"
            )

        # Determinism: build a second time into a different path; the
        # bytes must match. Without this, the .sha256 sidecar records a
        # fact about the run, not about the source — the integrity edge
        # is incomplete one rung above the vault.
        second_pyz = Path(td) / "bubble-second.pyz"
        proc2 = subprocess.run(
            [sys.executable, "-m", "bubble", "run", str(BUILD_SCRIPT),
             "--", "-o", str(second_pyz)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert proc2.returncode == 0, (
            f"second build failed (rc={proc2.returncode}): {proc2.stderr}"
        )
        h2 = hashlib.sha256()
        with second_pyz.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h2.update(chunk)
        second_digest = h2.hexdigest()
        assert second_digest == actual, (
            f"build is non-deterministic: first={actual} "
            f"second={second_digest}"
        )
        r.evidence.append(
            f"deterministic: two builds same source → identical "
            f"sha256 {actual[:16]}…"
        )

    r.passed = True


if __name__ == "__main__":
    run_test(
        claim="bubble can build its own deployment artifact via bubble run",
        fn=body,
    )
