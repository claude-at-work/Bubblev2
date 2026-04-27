"""Claim: substrate routing makes the loop load-bearing — the second
run starts smarter than the first.

Conventional intuition: a runtime makes a decision, the decision is
local to that run, the next run starts from scratch. Bubble's stance:
every routing decision becomes a fact in host.toml, and the next run
consults those facts before consulting the live probe. A
machine-level downgrade for one alias becomes the first answer the
next run reaches for, no probing required.

This test:
  - First resolution: an alias requests `dlmopen_isolated`. Today's
    runtime runs only in_process, so the router downgrades and
    records `substrate_downgraded` to host.toml.
  - Second resolution (fresh finder): the router sees the prior
    decision in host.toml and reuses it directly — Decision marked
    learned_from_history=True.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


def body(r: Result):
    from bubble import host, route

    stage_fake_package(name="numwidget", version="1.0.0", import_name="numwidget",
                       init_source='VERSION = "1.0.0"\n')
    stage_fake_package(name="numwidget", version="2.0.0", import_name="numwidget",
                       init_source='VERSION = "2.0.0"\n')

    # First run: explicit substrate request that today's runtime cannot
    # honor. Router downgrades, records, returns Decision.
    aliases = {
        "numwidget_isolated": ("numwidget", "1.0.0", "py3-none-any",
                               "dlmopen_isolated"),
    }

    failures_before = len(host.known_failures())

    with vault_finder(aliases=aliases) as finder:
        import numwidget_isolated  # type: ignore
        assert numwidget_isolated.VERSION == "1.0.0"
    r.evidence.append("first run: alias resolved, bytes loaded via downgrade")

    # The downgrade must have been recorded.
    failures_after = host.known_failures()
    downgrades = [
        f for f in failures_after
        if f.get("kind") == "substrate_downgraded"
        and f.get("target") == "numwidget_isolated"
    ]
    assert downgrades, \
        f"no substrate_downgraded entry recorded; saw kinds: " \
        f"{sorted({f.get('kind') for f in failures_after})}"
    detail = downgrades[-1].get("detail", "")
    assert "requested=dlmopen_isolated" in detail, f"detail malformed: {detail}"
    assert "actual=in_process" in detail, f"detail malformed: {detail}"
    r.evidence.append(
        f"first run: recorded substrate_downgraded "
        f"(dlmopen_isolated → in_process)"
    )

    # Second run: route() should consult history and shortcut.
    decision = route.route("numwidget_isolated", "dlmopen_isolated")
    assert decision.learned_from_history, \
        f"second-run decision not history-informed: {decision}"
    assert decision.actual == "in_process", \
        f"history reuse landed on wrong substrate: {decision}"
    assert decision.downgraded_from == "dlmopen_isolated"
    r.evidence.append(
        f"second run: decision learned_from_history=True; "
        f"actual={decision.actual} without re-probing"
    )

    # And the second run should NOT add another downgrade record (the
    # decision is reused, not re-recorded — the loop is informed, not
    # redundant).
    failures_check = host.known_failures()
    new_downgrades = [
        f for f in failures_check
        if f.get("kind") == "substrate_downgraded"
        and f.get("target") == "numwidget_isolated"
    ]
    assert len(new_downgrades) == len(downgrades), \
        f"second run added redundant downgrade record: " \
        f"{len(new_downgrades) - len(downgrades)} new entries"
    r.evidence.append(
        "second run did not double-record — load-bearing memory, not noise"
    )

    r.evidence.append(
        "→ probe → consult → record → consult: the four-step loop is "
        "weight-bearing on substrate routing"
    )
    r.passed = True


if __name__ == "__main__":
    run_test(
        "substrate routing closes the load-bearing loop: a first-run "
        "downgrade records to host.toml, a second-run resolution learns "
        "from history without re-probing, and no redundant entries "
        "accumulate — every run starts smarter than the last",
        body,
    )
