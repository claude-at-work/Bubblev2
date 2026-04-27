"""Differential evaluation across aliased versions — the verb that
turns the multi-version-coexistence substrate into an answer to the
question agent runtimes actually ask: *did upgrading this dependency
silently change the behavior I depend on?*

The shape:

    from bubble import AgentVault
    from bubble.tools import diff

    with AgentVault() as av:
        av.add("requests", version="2.31.0")
        av.add("requests", version="2.32.5")
        av.register("requests_old", real_name="requests", version="2.31.0",
                    wheel_tag="py3-none-any")
        av.register("requests_new", real_name="requests", version="2.32.5",
                    wheel_tag="py3-none-any")

        # Single-shot
        r = diff.compare(av, "m.utils.requote_uri('a b/c')",
                         aliases=["requests_old", "requests_new"])
        if not r.identical:
            for alias, outcome in r.results.items():
                ...

        # Multi-input differential fuzz
        r = diff.fuzz(av, "m.utils.requote_uri(x)",
                      aliases=["requests_old", "requests_new"],
                      n=500, strategy="strings")
        for inp, row in r.divergences:
            ...

        # Bisect across an ordered range
        r = diff.bisect(av, ["click_v0", "click_v1", "click_v2", "click_v3"],
                        "m.style('hello').encode()")
        # r.boundary: (alias_lo, alias_hi) — where the change first appears

The library returns structured results — no printing, no colors, no
terminal control. Callers (CLIs, REPLs, agent runtimes evaluating
upgrade decisions) format however they like.

The vault, the alias map, and the meta-finder are AgentVault's. This
module never touches sys.modules, sys.meta_path, or the SQLite index
directly. Differential evaluation is composition over the substrate, not
a parallel path to it.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


# ───────────────────────── result types ─────────────────────────


@dataclass
class CompareResult:
    """One evaluation per alias of a single expression.

    `results` maps alias name → ('ok', value) or ('err', ExcType, message).
    `identical` is True iff every alias produced the same fingerprint.
    """
    expr: str
    aliases: list[str]
    results: dict[str, tuple]
    identical: bool


@dataclass
class FuzzResult:
    """Differential fuzz across N inputs.

    `divergences`: list of (input, {alias → fingerprint}) for inputs where
    not all aliases agreed. `boundaries`: ordered alias-pair → divergent-
    input count, populated only if `aliases` was given in a meaningful
    order (e.g. oldest-to-newest)."""
    expr: str
    aliases: list[str]
    n_inputs: int
    strategy: str
    seed: int
    divergences: list[tuple[Any, dict[str, tuple]]]
    boundaries: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def all_agreed(self) -> bool:
        return not self.divergences


@dataclass
class BisectResult:
    """Binary search localizing a behavioral change to an alias pair.

    `boundary`: (alias_lo, alias_hi) — the change first appears between
    these two adjacent aliases in the ordered range. `evaluations`: number
    of expression evaluations performed. `endpoint_fingerprints` records
    the values at the range endpoints, for explanation."""
    expr: str
    ordered_aliases: list[str]
    boundary: Optional[tuple[str, str]]
    evaluations: int
    endpoint_fingerprints: dict[str, tuple]
    converged: bool


# ───────────────────────── public verbs ─────────────────────────


def compare(av, expr: str, aliases: list[str]) -> CompareResult:
    """Evaluate `expr` once against each alias's module. The expression
    has the alias module bound to `m`; e.g. `m.__version__`,
    `set(dir(m))`, `m.utils.requote_uri('a b')`.

    Returns a CompareResult; the caller decides how to surface
    differences."""
    if len(aliases) < 2:
        raise ValueError(
            f"compare needs at least 2 aliases; got {len(aliases)}"
        )
    results: dict[str, tuple] = {}
    fingerprints: list[tuple] = []
    redact = _alias_redactor(av)
    for alias in aliases:
        outcome = _eval_one(av, alias, expr, x=None, redact=redact)
        results[alias] = outcome
        fingerprints.append(_fingerprint_outcome(outcome))
    identical = len(set(fingerprints)) == 1
    return CompareResult(
        expr=expr, aliases=list(aliases), results=results,
        identical=identical,
    )


def fuzz(
    av,
    expr: str,
    aliases: list[str],
    *,
    n: int = 100,
    strategy: str = "strings",
    seed: int = 0,
    inputs: Optional[Iterable[Any]] = None,
) -> FuzzResult:
    """Generate `n` inputs, evaluate `expr` against each alias, return a
    FuzzResult that lists divergent inputs and ranks the alias pairs
    where divergence first appears.

    The expression takes two free names: `m` (the alias module) and `x`
    (the fuzzed input). E.g. `m.utils.requote_uri(x)`,
    `m.style(x, fg='red')`.

    `strategy` is one of 'strings', 'ints', 'floats', 'bytes'. Pass
    `inputs` to supply your own corpus instead — useful when you've
    already grepped a real codebase for the relevant call sites and
    want to fuzz against the actual payload shapes the package sees in
    production."""
    if len(aliases) < 2:
        raise ValueError(
            f"fuzz needs at least 2 aliases; got {len(aliases)}"
        )
    if inputs is None:
        corpus = list(_make_inputs(strategy, n, seed))
    else:
        corpus = list(inputs)
        n = len(corpus)
    redact = _alias_redactor(av)

    rows: list[tuple[Any, dict[str, tuple]]] = []
    for x in corpus:
        per_alias: dict[str, tuple] = {}
        for alias in aliases:
            per_alias[alias] = _eval_one(av, alias, expr, x=x, redact=redact)
        rows.append((x, per_alias))

    divergences: list[tuple[Any, dict[str, tuple]]] = []
    for x, per_alias in rows:
        fps = {a: _fingerprint_outcome(o) for a, o in per_alias.items()}
        if len(set(fps.values())) > 1:
            divergences.append((x, fps))

    boundaries: dict[tuple[str, str], int] = {}
    for _x, fps in divergences:
        for k in range(len(aliases) - 1):
            a, b = aliases[k], aliases[k + 1]
            if fps[a] != fps[b]:
                boundaries[(a, b)] = boundaries.get((a, b), 0) + 1

    return FuzzResult(
        expr=expr, aliases=list(aliases), n_inputs=n, strategy=strategy,
        seed=seed, divergences=divergences, boundaries=boundaries,
    )


def bisect(
    av,
    ordered_aliases: list[str],
    expr: str,
    *,
    input_value: Any = None,
) -> BisectResult:
    """Binary-search the ordered alias range to find the adjacent pair
    where `expr`'s output first changes.

    The endpoints must produce different fingerprints, otherwise there
    is nothing to bisect (`converged=True`, `boundary=None`).

    The expression takes `m` (alias module) and optionally `x` (bound
    to `input_value` if given, otherwise unbound)."""
    if len(ordered_aliases) < 2:
        raise ValueError(
            f"bisect needs at least 2 ordered aliases; got "
            f"{len(ordered_aliases)}"
        )
    redact = _alias_redactor(av)

    def evaluate(alias: str) -> tuple:
        return _fingerprint_outcome(
            _eval_one(av, alias, expr, x=input_value, redact=redact)
        )

    fp_lo = evaluate(ordered_aliases[0])
    fp_hi = evaluate(ordered_aliases[-1])
    endpoints = {ordered_aliases[0]: fp_lo, ordered_aliases[-1]: fp_hi}
    if fp_lo == fp_hi:
        return BisectResult(
            expr=expr, ordered_aliases=list(ordered_aliases),
            boundary=None, evaluations=2,
            endpoint_fingerprints=endpoints, converged=True,
        )

    lo, hi = 0, len(ordered_aliases) - 1
    evaluations = 2
    while hi - lo > 1:
        mid = (lo + hi) // 2
        fp_mid = evaluate(ordered_aliases[mid])
        evaluations += 1
        if fp_mid == fp_lo:
            lo = mid
        else:
            hi = mid

    return BisectResult(
        expr=expr, ordered_aliases=list(ordered_aliases),
        boundary=(ordered_aliases[lo], ordered_aliases[hi]),
        evaluations=evaluations,
        endpoint_fingerprints=endpoints, converged=True,
    )


# ───────────────────────── internals ─────────────────────────


def _alias_redactor(av) -> Callable[[str], str]:
    """Build a callable that strips registered alias names out of a
    string. Without this, exception messages embedding the calling
    module's __name__ ('module \\'click_v0.utils\\'…') become distinct
    fingerprints across aliases that actually raised the same error —
    the boundary table reports spurious 100% divergence. Lesson learned
    from the bubble-bubble REPL prototype."""
    aliases = sorted(av.registered_tools().keys(), key=len, reverse=True)
    if not aliases:
        return lambda s: s

    def redact(s: str) -> str:
        if not s:
            return s
        for alias in aliases:
            s = s.replace(alias, "<alias>")
        return s
    return redact


def _eval_one(av, alias: str, expr: str, *, x: Any,
              redact: Callable[[str], str]) -> tuple:
    """Evaluate `expr` against a single alias. Returns ('ok', value) or
    ('err', exc_type_name, redacted_message). Acquires the module via
    AgentVault.tool() so we never bypass the substrate ladder."""
    try:
        mod = av.tool(alias)
    except Exception as exc:
        return ("err", type(exc).__name__,
                redact(str(exc))[:200] or repr(exc)[:200])
    local_ns = {"m": mod, "x": x}
    try:
        return ("ok", eval(expr, local_ns))
    except Exception as exc:
        return ("err", type(exc).__name__,
                redact(str(exc))[:200] or repr(exc)[:200])


def _fingerprint_outcome(outcome: tuple) -> tuple:
    """Turn an _eval_one outcome into a stable, hashable fingerprint
    suitable for cross-alias comparison."""
    if outcome[0] == "ok":
        return ("ok", _fingerprint(outcome[1]))
    return outcome  # 'err' tuples are already hashable


def _fingerprint(val: Any) -> tuple:
    """Hashable, stable representation of a value. Sets/dicts are
    sorted; lists/tuples preserve order; falls back to repr."""
    try:
        if isinstance(val, (str, int, float, bool, bytes, type(None))):
            return ("v", val)
        if isinstance(val, (list, tuple)):
            return (type(val).__name__,
                    tuple(_fingerprint(x) for x in val))
        if isinstance(val, set):
            return ("set", tuple(sorted(_fingerprint(x) for x in val)))
        if isinstance(val, frozenset):
            return ("frozenset",
                    tuple(sorted(_fingerprint(x) for x in val)))
        if isinstance(val, dict):
            return ("dict", tuple(sorted(
                (_fingerprint(k), _fingerprint(v))
                for k, v in val.items())))
        return ("r", repr(val))
    except Exception:
        return ("r", repr(val)[:200])


def _make_inputs(strategy: str, n: int, seed: int):
    rng = random.Random(seed)
    if strategy == "strings":
        alphabet = ("abcdefghijklmnopqrstuvwxyz0123456789"
                    "/?#:%&=+@.- ")
        for _ in range(n):
            ln = rng.randint(0, 40)
            yield "".join(rng.choice(alphabet) for _ in range(ln))
    elif strategy == "ints":
        for _ in range(n):
            kind = rng.random()
            if kind < 0.4:
                yield rng.randint(-100, 100)
            elif kind < 0.8:
                yield rng.randint(-10**6, 10**6)
            else:
                yield rng.randint(-2**31, 2**31)
    elif strategy == "floats":
        for _ in range(n):
            yield rng.uniform(-1e6, 1e6)
    elif strategy == "bytes":
        for _ in range(n):
            ln = rng.randint(0, 40)
            yield bytes(rng.randint(0, 255) for _ in range(ln))
    else:
        raise ValueError(
            f"unknown strategy {strategy!r}; "
            f"expected one of strings, ints, floats, bytes"
        )
