"""Claim: when two distributions both claim the same import name, the
contention is logged as a structured audit event. First-claim semantics
remain in effect for resolution, but the contention is observable instead of
a silent accident of vault-add order.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    from bubble.vault import store

    store.top_level_contentions.clear()

    stage_fake_package(
        name="opencv-python", version="4.10.0", import_name="cv2",
        init_source='SOURCE = "opencv-python"',
    )
    # No contention yet — only one claimant.
    assert store.top_level_contentions == [], (
        f"first claim should not log contention, got: "
        f"{store.top_level_contentions}"
    )

    stage_fake_package(
        name="opencv-python-headless", version="4.10.0", import_name="cv2",
        init_source='SOURCE = "opencv-python-headless"',
    )

    contentions = list(store.top_level_contentions)
    assert len(contentions) == 1, f"expected 1 contention entry, got {contentions}"
    entry = contentions[0]
    assert entry["import_name"] == "cv2"
    assert entry["incoming"][0] == "opencv-python-headless"
    assert any(
        existing[0] == "opencv-python" for existing in entry["existing"]
    ), entry["existing"]
    assert entry.get("incoming_sha256"), "contention entry missing incoming_sha256"

    r.evidence.append(f"first claimant:  opencv-python (no log)")
    r.evidence.append(f"second claimant: opencv-python-headless")
    r.evidence.append(f"contention recorded: import_name={entry['import_name']}")
    r.evidence.append(f"existing: {entry['existing']}")
    r.evidence.append(f"incoming sha256: {entry['incoming_sha256'][:16]}…")
    r.evidence.append("→ collisions are observable, not silent")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "import-name collisions across distributions emit a structured "
        "contention log entry — silent accident becomes observable event",
        body,
    )
