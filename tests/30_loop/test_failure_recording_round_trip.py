"""Claim: a runtime-recorded failure round-trips — write through host.record_failure,
read through host.known_failures, find via host.is_known_failure.

This is the half of the probe→consult→record→consult loop the README admits
is currently the smaller half of the work: the recording channel exists, the
substrate-routing consultation does not. So this test does NOT claim that the
recording *changes behavior on the next run* — that's an xfail (see the next
test in this dir, when written). What this test claims is the narrower thing:
the channel is plumbed end-to-end. Round-trip works.

If this test is green and `test_recorded_failure_alters_next_run_strategy.py`
is red/xfail, the gap is named precisely: the loop is half-closed, and we
know which half.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble import host, probe, config

    config.ensure_dirs()
    # Seed host.toml with a real probe so we're round-tripping through the
    # actual file format, not a stub. (record_failure tolerates either, but
    # this is a more honest test.)
    host.host_toml_path().write_text(probe.to_toml(probe.run_all()))

    # Three failures the runtime might plausibly record
    host.record_failure("pypi_fetch_failed", "nonexistent-package-xyz",
                        "HTTP 404 from index")
    host.record_failure("wheel_load_segfault", "broken-pkg==9.9.9",
                        "received SIGSEGV during dlopen")
    host.record_failure("dlmopen_unavailable", "numpy",
                        "preconditions not met on this kernel")

    failures = host.known_failures()
    assert len(failures) >= 3, f"expected at least 3 failures, got {len(failures)}"

    kinds = {f["kind"] for f in failures}
    assert {"pypi_fetch_failed", "wheel_load_segfault",
            "dlmopen_unavailable"}.issubset(kinds), \
        f"missing kinds; got {kinds}"

    f = host.is_known_failure("wheel_load_segfault", "broken-pkg==9.9.9")
    assert f is not None, "is_known_failure should find the recorded entry"
    assert "SIGSEGV" in f["detail"], f"detail not preserved: {f['detail']!r}"

    f_missing = host.is_known_failure("pypi_fetch_failed", "definitely-not-recorded")
    assert f_missing is None, "is_known_failure should return None for unrecorded"

    r.evidence.append(f"recorded {len(failures)} failures via host.record_failure")
    r.evidence.append(f"distinct kinds: {sorted(kinds)}")
    r.evidence.append(f"round-tripped detail: {f['detail']!r}")
    r.evidence.append("→ channel is plumbed; record→consult half of the loop works")
    r.evidence.append("(open: the *next-run-alters-strategy* half is not yet load-bearing)")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "runtime failures round-trip through host.toml: write via record_failure, "
        "read via known_failures, find via is_known_failure",
        body,
    )
