#!/usr/bin/env python3
"""Bubble test runner.

Each test file is a self-contained script. The runner invokes each as a
subprocess so that BUBBLE_HOME, sys.meta_path, and sys.modules are isolated
between tests. The test prints a JSON line tagged __BUBBLE_TEST_RESULT__;
the runner slurps it.

Output:
  - stdout: a tabular summary of every test
  - tests/RESULTS.md: the gallery — claim, evidence, timing — for reading

Usage:
  python3 tests/run.py                 # run everything
  python3 tests/run.py 10_breakers     # filter by tier
  python3 tests/run.py --no-md         # skip RESULTS.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
RESULT_TAG = "__BUBBLE_TEST_RESULT__"


def discover(filter_str: str | None) -> list[Path]:
    out = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        if filter_str and filter_str not in str(path.relative_to(TESTS_DIR)):
            continue
        out.append(path)
    return out


def run_one(path: Path) -> dict:
    rel = path.relative_to(TESTS_DIR)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(path)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_TAG):
            payload = json.loads(line[len(RESULT_TAG):])
            break

    if payload is None:
        return {
            "claim": str(rel),
            "passed": False,
            "skipped": None,
            "error": (
                f"test produced no {RESULT_TAG} line.\n"
                f"--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}"
            ),
            "evidence": [],
            "elapsed_ms": 0,
            "path": str(rel),
        }
    payload["path"] = str(rel)
    if proc.returncode != 0 and payload.get("error") is None and payload.get("passed"):
        payload["passed"] = False
        payload["error"] = f"non-zero exit ({proc.returncode}); stderr:\n{proc.stderr}"
    return payload


def status_glyph(p: dict) -> str:
    if p.get("skipped"):
        return "○"
    if p.get("passed"):
        return "✓"
    return "✗"


def print_summary(results: list[dict]) -> None:
    width = max(len(r["path"]) for r in results) if results else 0
    for r in results:
        glyph = status_glyph(r)
        ms = r.get("elapsed_ms", 0)
        print(f"  {glyph}  {r['path']:<{width}}  {ms:>5}ms  {r['claim']}")
    n_pass = sum(1 for r in results if r.get("passed"))
    n_skip = sum(1 for r in results if r.get("skipped"))
    n_fail = sum(1 for r in results if not r.get("passed") and not r.get("skipped"))
    print()
    print(f"  {n_pass} passed  {n_fail} failed  {n_skip} skipped")


def write_results_md(results: list[dict]) -> Path:
    out_path = TESTS_DIR / "RESULTS.md"
    n_pass = sum(1 for r in results if r.get("passed"))
    n_skip = sum(1 for r in results if r.get("skipped"))
    n_fail = sum(1 for r in results if not r.get("passed") and not r.get("skipped"))

    lines: list[str] = []
    lines.append("# Bubble — test gallery")
    lines.append("")
    lines.append(
        "Each entry below is an architectural claim that was tested by running "
        "real code against the actual `bubble` package on this machine. "
        "The tests are also exhibits — read them to learn what the system does."
    )
    lines.append("")
    lines.append(f"_Run: {datetime.now().isoformat(timespec='seconds')} — "
                 f"{n_pass} passed, {n_fail} failed, {n_skip} skipped._")
    lines.append("")
    lines.append("---")
    lines.append("")

    for r in results:
        glyph = status_glyph(r)
        lines.append(f"## {glyph} {r['claim']}")
        lines.append("")
        lines.append(f"`{r['path']}` — {r.get('elapsed_ms', 0)} ms")
        lines.append("")
        if r.get("skipped"):
            lines.append(f"**Skipped:** {r['skipped']}")
            lines.append("")
        elif not r.get("passed"):
            lines.append("**Failed.**")
            lines.append("")
            err = r.get("error", "(no error captured)")
            lines.append("```")
            lines.append(err.rstrip())
            lines.append("```")
            lines.append("")
        if r.get("evidence"):
            lines.append("```")
            for line in r["evidence"]:
                lines.append(line)
            lines.append("```")
            lines.append("")
    out_path.write_text("\n".join(lines))
    return out_path


def main() -> int:
    args = sys.argv[1:]
    filter_str = None
    write_md = True
    for a in args:
        if a == "--no-md":
            write_md = False
        elif not a.startswith("-"):
            filter_str = a

    paths = discover(filter_str)
    if not paths:
        print("no tests found", file=sys.stderr)
        return 2

    print(f"running {len(paths)} test(s)...\n")
    results = [run_one(p) for p in paths]
    print_summary(results)

    if write_md:
        out = write_results_md(results)
        print(f"\nwrote {out.relative_to(REPO_ROOT)}")

    n_fail = sum(1 for r in results if not r.get("passed") and not r.get("skipped"))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
