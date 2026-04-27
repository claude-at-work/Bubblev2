"""Substrate routing — the decision layer between alias-as-declared and
alias-as-resolved.

A deployment manifest can declare `substrate = "dlmopen_isolated"` for an
alias. Whether that substrate is actually available on the running
machine is a probe-time fact. Whether it has worked for this alias on
this machine is a runtime fact accumulated in host.toml. The router
holds the two together: takes the declared substrate, consults the host
portrait + prior history, and returns a Decision with what to actually
do.

The substrate ladder, most-isolated to least:

    subprocess  >  dlmopen_isolated  >  sub_interpreter  >  in_process

A "downgrade" walks down the ladder when the requested substrate isn't
available. The downgrade is recorded — not silently absorbed — because
walking down the ladder reduces isolation, and if the alias was
declaring its substrate for an isolation reason (two-numpy, two-torch),
the downgraded form is likely to fail at import time. That second
failure becomes another fact, and the chain in host.toml tells the
operator exactly what's mismatched.

Today's implementation runs only `in_process`. The other substrates
return Decisions that record substrate_not_implemented and downgrade to
in_process. The architecture is in place; the substrate handlers
themselves are the stretch work behind this thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import host


# Most-isolated → least-isolated.
SUBSTRATE_LADDER = (
    "subprocess",
    "dlmopen_isolated",
    "sub_interpreter",
    "in_process",
)


@dataclass
class Decision:
    """The router's verdict for one alias resolution.

    `actual` is the substrate that will be used. If `requested` differs,
    `downgraded_from` carries the original; the caller records this as
    `substrate_downgraded`. If `actual` is None the alias cannot be
    served on this machine — the caller records `substrate_unavailable`
    and refuses the spec.

    `learned_from_history` is True when the decision was made by
    consulting host.toml's prior records rather than the live probe —
    the second-run-starts-smarter property.
    """
    alias: str
    requested: Optional[str]
    actual: Optional[str]
    downgraded_from: Optional[str] = None
    learned_from_history: bool = False
    reason: str = ""


def route(alias: str, requested: Optional[str]) -> Decision:
    """Decide which substrate will host this alias.

    Procedure:
      1. If host.toml records that this alias was previously routed to
         a substrate other than the requested one (with success or with
         a known-bad downgrade), reuse that decision. The accumulated
         history is the second-run intelligence.
      2. Otherwise, if the requested substrate is `in_process` or None,
         route there directly.
      3. Otherwise, consult host.has_substrate(requested). If true,
         record the choice and route. (Today's stub: non-in_process
         substrates aren't implemented, so we fall through to step 4.)
      4. Walk down SUBSTRATE_LADDER from `requested` until we find a
         substrate the host has AND we know how to run. Record the
         downgrade.
    """
    # Step 1: history-informed shortcut.
    learned = _consult_history(alias, requested)
    if learned is not None:
        return learned

    # Step 2: in_process is the trivial path.
    if requested in (None, "in_process"):
        return Decision(alias=alias, requested=requested, actual="in_process",
                        reason="default in_process route")

    # Step 3: requested substrate available + implemented?
    if host.has_substrate(requested) and _is_implemented(requested):
        return Decision(alias=alias, requested=requested, actual=requested,
                        reason=f"{requested} available and implemented on this host")

    # Step 4: downgrade. Walk down the ladder until we find a substrate
    # we can both reach and run.
    try:
        start = SUBSTRATE_LADDER.index(requested)
    except ValueError:
        # Unknown substrate name — treat as "couldn't even start".
        return Decision(
            alias=alias, requested=requested, actual="in_process",
            downgraded_from=requested,
            reason=f"unknown substrate {requested!r}; downgraded to in_process",
        )

    for candidate in SUBSTRATE_LADDER[start + 1:]:
        if (candidate == "in_process" or
                (host.has_substrate(candidate) and _is_implemented(candidate))):
            reason_parts = []
            if not host.has_substrate(requested):
                reason_parts.append(f"{requested} not on this host")
            elif not _is_implemented(requested):
                # Pull the handler's status so host.toml learns *what*
                # isn't ready, not just that it isn't.
                reason_parts.append(
                    f"{requested}: {_substrate_status(requested)}"
                )
            reason_parts.append(f"using {candidate}")
            return Decision(
                alias=alias, requested=requested, actual=candidate,
                downgraded_from=requested,
                reason="; ".join(reason_parts),
            )

    return Decision(
        alias=alias, requested=requested, actual=None,
        downgraded_from=requested,
        reason=f"no substrate available; even in_process could not be reached",
    )


def record_decision(decision: Decision) -> None:
    """Persist a routing decision to host.toml so subsequent runs can
    consult it. Distinct kinds for distinct outcomes:

      - downgrade → kind=substrate_downgraded
      - unreachable → kind=substrate_unavailable
      - clean route → not recorded (no signal worth keeping)
    """
    if decision.actual is None:
        host.record_failure(
            "substrate_unavailable", decision.alias,
            decision.reason,
        )
    elif decision.downgraded_from:
        host.record_failure(
            "substrate_downgraded", decision.alias,
            f"requested={decision.downgraded_from} actual={decision.actual}; "
            f"{decision.reason}",
        )


def _consult_history(alias: str, requested: Optional[str]) -> Optional[Decision]:
    """If host.toml shows a prior downgrade for this (alias, requested),
    skip the live probe and reuse the accumulated answer."""
    if not requested or requested == "in_process":
        return None
    for f in reversed(host.known_failures()):
        if f.get("kind") != "substrate_downgraded":
            continue
        if f.get("target") != alias:
            continue
        detail = f.get("detail", "")
        # Detail format from record_decision: "requested=X actual=Y; ..."
        if f"requested={requested}" not in detail:
            continue
        actual = None
        for token in detail.split():
            if token.startswith("actual="):
                actual = token.split("=", 1)[1].rstrip(";")
                break
        if actual:
            return Decision(
                alias=alias, requested=requested, actual=actual,
                downgraded_from=requested,
                learned_from_history=True,
                reason=f"reusing prior route from host.toml: {requested}→{actual}",
            )
    return None


def _is_implemented(substrate: str) -> bool:
    """Substrate handlers register through bubble.substrate. The
    handler module reports whether it can complete an end-to-end
    routing today — most are partially shipped (the dlmopen_isolated
    namespace and interpreter initialize, but the cross-namespace proxy
    module bridge isn't yet built). Until full_routing_implemented()
    is True, the router downgrades; the downgrade reason carries the
    handler's status string so host.toml records what's missing, not
    just that it's missing.
    """
    from . import substrate as substrate_pkg
    return substrate_pkg.is_implemented(substrate)


def _substrate_status(substrate: str) -> str:
    from . import substrate as substrate_pkg
    return substrate_pkg.status(substrate)
