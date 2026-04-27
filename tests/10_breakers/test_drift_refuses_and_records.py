"""Claim: vault drift refuses the lookup AND lands in host.toml — the
first cut where the closed loop becomes load-bearing instead of decorative.

Conventional intuition: a content-addressed store needs a verify command
the operator runs occasionally. Bubble's stance is stronger: the store
verifies on every read, refuses on drift, and the refusal becomes future
intelligence — a `[[failures]]` entry the next `bubble host` invocation
surfaces. The vault doesn't grow opinions about what to do; the host
portrait grows facts about what happened.

This test stages a synthetic package, verifies the vault_files rows
exist clean, mutates a file in the vault tree, and asserts:
  - meta_finder._lookup refuses to resolve the import
  - host.toml gains a `[[failures]]` entry of kind `vault_drift_modified`
  - the kind is in the FAILURE_KINDS vocabulary
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


def body(r: Result):
    from bubble.vault import store
    from bubble import host

    name, version, wheel_tag, vault_path = stage_fake_package(
        name="canary",
        version="1.0.0",
        import_name="canary",
        init_source='VERSION = "1.0.0"\nimport sys\n',
    )

    # The C1 commit-time walk should have populated vault_files rows.
    report = store.verify(name, version, wheel_tag)
    assert report.had_index, "vault_files rows missing — commit-time walk didn't fire"
    assert report.clean, f"fresh vault should verify clean: {report}"
    r.evidence.append(
        f"clean verify: {len(report.matched)} matched, "
        f"{len(report.drifted)} drifted, {len(report.missing)} missing"
    )

    # Mutate a vaulted file. This is the drift event — bytes the vault
    # vouched for at commit are no longer the bytes on disk.
    init_file = vault_path / "canary" / "__init__.py"
    init_file.write_text(init_file.read_text() + "\n# tampered\n")

    drifted = store.verify(name, version, wheel_tag)
    assert not drifted.clean, "verify missed the mutation"
    drift_kinds = {kind for _rel, kind in drifted.drifted}
    assert "vault_drift_modified" in drift_kinds, \
        f"expected vault_drift_modified in {drift_kinds}"
    r.evidence.append(f"drift verify: drifted={drifted.drifted}")

    # The kind must be in the warp's vocabulary — that's what makes the
    # loom hold.
    assert host.is_known_kind("vault_drift_modified"), \
        "vault_drift_modified missing from FAILURE_KINDS"

    # Now go through the meta-finder. The lookup must refuse and the
    # failure must round-trip into host.toml.
    with vault_finder() as finder:
        spec = finder.find_spec("canary", None)
        assert spec is None, \
            "drift should have refused the lookup; got a spec"

    failures = host.known_failures()
    drift_entries = [f for f in failures if f.get("kind") == "vault_drift_modified"]
    assert drift_entries, \
        f"no vault_drift_modified failure recorded; saw kinds: " \
        f"{[f.get('kind') for f in failures]}"
    target = drift_entries[-1].get("target", "")
    assert "canary" in target and "1.0.0" in target, \
        f"target malformed: {target!r}"
    r.evidence.append(f"refused lookup; recorded {len(drift_entries)} drift entries")
    r.evidence.append(f"  most recent target: {target}")

    # Per-process cache: a second lookup in the same process must not
    # re-verify (we'd see double-recorded failures).
    before = len(host.known_failures())
    with vault_finder() as finder2:
        finder2._verified[(name, version, wheel_tag)] = False  # warmed
        finder2.find_spec("canary", None)
    after = len(host.known_failures())
    assert after == before, \
        f"cached refusal re-recorded; before={before}, after={after}"
    r.evidence.append("per-process cache prevents re-recording on repeat lookup")

    r.passed = True


if __name__ == "__main__":
    run_test(
        "vault drift refuses the lookup at the meta-finder AND surfaces a "
        "[[failures]] entry of kind vault_drift_modified in host.toml — "
        "the first place the closed loop is load-bearing rather than "
        "decorative; cached per-process so repeat lookups don't double-record",
        body,
    )
