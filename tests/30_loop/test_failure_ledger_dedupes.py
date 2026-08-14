"""Claim: host.ledger() dedupes repeat (kind, target) failures into a single
tallied row, and host.ledger_by_kind() rolls that up further into a
category-level count — without touching the raw [[failures]] log, which
stays append-only and complete.

This is a read-side view, not a new store: the same 7 record_failure()
calls that produce 7 raw log lines should collapse to fewer ledger rows
with counts that sum back to 7.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble import host, probe, config

    config.ensure_dirs()
    host.host_toml_path().write_text(probe.to_toml(probe.run_all()))

    # Same offender, three times — should collapse to one ledger row, count=3.
    host.record_failure("permission_denied", "shells/torch-cuda/lib/torch",
                        "PermissionError: [Errno 13] Permission denied")
    host.record_failure("permission_denied", "shells/torch-cuda/lib/torch",
                        "PermissionError: [Errno 13] Permission denied")
    host.record_failure("permission_denied", "shells/torch-cuda/lib/torch",
                        "PermissionError: [Errno 13] Permission denied")

    # A different target, same kind — distinct ledger row, but same category.
    host.record_failure("permission_denied", "keep/dotfiles/.zshrc",
                        "PermissionError: [Errno 13] Permission denied")

    # A different kind entirely.
    host.record_failure("hardware_variant_mismatch", "train.py",
                        "CUDA driver version is insufficient for CUDA runtime version")
    host.record_failure("hardware_variant_mismatch", "train.py",
                        "CUDA driver version is insufficient for CUDA runtime version")

    host.record_failure("path_conflict", "keep/dotfiles/.zshrc",
                        "keep=dotfiles: non-symlink occupies target")

    raw = host.known_failures()
    assert len(raw) == 7, f"raw log should keep every occurrence, got {len(raw)}"

    entries = host.ledger()
    assert sum(e["count"] for e in entries) == 7, \
        f"ledger counts should sum back to the raw total, got {[e['count'] for e in entries]}"

    top = entries[0]
    assert top["kind"] == "permission_denied" and top["count"] == 3, \
        f"repeat offender should sort first with count=3, got {top}"
    assert "Permission denied" in top["last_detail"]

    by_target = {(e["kind"], e["target"]): e["count"] for e in entries}
    assert by_target[("permission_denied", "shells/torch-cuda/lib/torch")] == 3
    assert by_target[("permission_denied", "keep/dotfiles/.zshrc")] == 1
    assert by_target[("hardware_variant_mismatch", "train.py")] == 2
    assert by_target[("path_conflict", "keep/dotfiles/.zshrc")] == 1
    assert len(entries) == 4, f"expected 4 distinct (kind, target) rows, got {len(entries)}"

    by_kind = {e["kind"]: e for e in host.ledger_by_kind()}
    assert by_kind["permission_denied"]["count"] == 4
    assert by_kind["permission_denied"]["distinct_targets"] == 2
    assert by_kind["hardware_variant_mismatch"]["count"] == 2
    assert by_kind["hardware_variant_mismatch"]["distinct_targets"] == 1
    assert by_kind["path_conflict"]["count"] == 1

    r.evidence.append(f"7 record_failure() calls -> {len(raw)} raw log lines "
                      f"(unchanged, append-only)")
    r.evidence.append(f"-> {len(entries)} deduped (kind, target) ledger rows, "
                      f"counts sum to {sum(e['count'] for e in entries)}")
    r.evidence.append(f"-> {len(by_kind)} category rows via ledger_by_kind(); "
                      f"permission_denied: count={by_kind['permission_denied']['count']}, "
                      f"distinct_targets={by_kind['permission_denied']['distinct_targets']}")
    r.evidence.append("repeat offender (torch-cuda symlink denied x3) sorts first")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "host.ledger() dedupes repeat (kind, target) failures with a count; "
        "ledger_by_kind() tallies at the category level; raw log stays complete",
        body,
    )
