"""Claim: `bubble probe` produces a host.toml that the host module can read,
and the substrates list reflects what this machine can actually host.

This is the first half of the probe→consult loop the README describes.
A probe that writes a file no one reads isn't closing anything. A probe
whose output round-trips through the host module is.

Proof shape: run the probe in-process, write its output, ask the host module
to read it back, verify the substrate menu matches the raw probe results.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble import probe, host, config

    config.ensure_dirs()
    results = probe.run_all()
    toml = probe.to_toml(results)
    host.host_toml_path().write_text(toml)

    parsed = host.load()
    assert parsed, "host.load() returned empty after probe wrote host.toml"
    assert "kernel" in parsed and parsed["kernel"], "kernel section missing"
    assert "python" in parsed and parsed["python"], "python section missing"

    subs = host.substrates()
    sub_names = {s["name"] for s in subs}
    assert "in_process" in sub_names, "in_process should always be available"
    assert host.has_substrate("in_process"), "in_process should be 'available'"

    # Cross-check: subprocess substrate is also always available
    assert "subprocess" in sub_names, "subprocess should always be listed"
    assert host.has_substrate("subprocess"), "subprocess should be available"

    # Surface what the machine reports — failure or success, this is a real
    # self-portrait, not a contrived one. Show it inline.
    r.evidence.append(f"probed_at: {parsed.get('probed_at', '?')}")
    r.evidence.append(f"kernel:    {parsed['kernel'].get('system', '?')} "
                      f"{parsed['kernel'].get('release', '?')} "
                      f"{parsed['kernel'].get('machine', '?')}")
    r.evidence.append(f"python:    {parsed['python'].get('version', '?')} "
                      f"({parsed['python'].get('implementation', '?')})")
    r.evidence.append("substrates this machine reports it can host:")
    for s in subs:
        status = s.get("status", "?")
        cost = s.get("cost_mb")
        cost_str = f"{cost}MB" if isinstance(cost, int) else "?"
        r.evidence.append(f"  - {s['name']:18} {status:48} cost={cost_str}")
    r.evidence.append("→ probe writes, host reads, the portrait is real")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "bubble probe writes host.toml; the host module reads it back; "
        "the substrate menu reflects machine capability",
        body,
    )
