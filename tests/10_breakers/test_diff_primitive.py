"""Claim: `bubble.tools.diff` is the differential-evaluation verb on top
of AgentVault. Given aliased versions of a package, `compare()` reports
per-alias evaluations of one expression, `fuzz()` reports divergent
inputs and ranks regression boundaries, and `bisect()` localizes a
behavioral change to an adjacent alias pair. The substrate underneath is
just AgentVault — no new path to the vault, no parallel alias registry.

Why this matters: the README claim is *agent runtimes register tools by
alias with declarable isolation, two versions of one dist coexist as
differently-shaped tools.* The natural follow-on question — *which
behaviors actually changed between the two?* — was previously a thing
the consuming runtime had to build itself. With this module, bubble
answers it directly. Composition over the substrate, not parallel
implementation.

Proof shape:
  1. Stage three synthetic versions of `widget` whose `Widget.label()`
     differs between v1 and v2-and-up (a behavioral change at the v1→v2
     boundary), plus a v0 that matches v1 (no change there). This is
     deliberately a non-trivial change pattern: the bisect should
     localize it precisely, not just say 'something changed'.
  2. Register all three under ordered aliases and exercise compare,
     fuzz, and bisect.
  3. Assert each verb produces the expected structured result — no
     terminal-formatting in the library, just data the caller can act
     on.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, Result


def body(r: Result):
    # v0 and v1 both return 'small'; v2 and later return 'compact'.
    # That makes the v1→v2 boundary the answer bisect must localize.
    stage_fake_package(
        name="widget", version="1.0.0", import_name="widget",
        init_source='''
            class Widget:
                @staticmethod
                def label(x): return "small"
        ''',
    )
    stage_fake_package(
        name="widget", version="1.1.0", import_name="widget",
        init_source='''
            class Widget:
                @staticmethod
                def label(x): return "small"
        ''',
    )
    stage_fake_package(
        name="widget", version="2.0.0", import_name="widget",
        init_source='''
            class Widget:
                @staticmethod
                def label(x): return "compact"
        ''',
    )

    from bubble import AgentVault
    from bubble.tools import diff as diff_mod

    with AgentVault() as av:
        av.register("widget_v0", real_name="widget", version="1.0.0",
                    wheel_tag="py3-none-any")
        av.register("widget_v1", real_name="widget", version="1.1.0",
                    wheel_tag="py3-none-any")
        av.register("widget_v2", real_name="widget", version="2.0.0",
                    wheel_tag="py3-none-any")

        # ── compare: v0 vs v2, single expression ─────────────────────
        cr = diff_mod.compare(
            av, "m.Widget.label(None)",
            aliases=["widget_v0", "widget_v2"],
        )
        assert cr.identical is False, "v0 and v2 should differ on label()"
        assert cr.results["widget_v0"] == ("ok", "small")
        assert cr.results["widget_v2"] == ("ok", "compact")
        r.evidence.append(
            f"compare(v0, v2): "
            f"v0={cr.results['widget_v0']} v2={cr.results['widget_v2']} "
            f"identical={cr.identical}"
        )

        # ── compare: v0 vs v1, identical behavior ────────────────────
        cr_same = diff_mod.compare(
            av, "m.Widget.label(None)",
            aliases=["widget_v0", "widget_v1"],
        )
        assert cr_same.identical is True, \
            "v0 and v1 hit the same label()"
        r.evidence.append(
            f"compare(v0, v1): identical={cr_same.identical} "
            f"(both report {cr_same.results['widget_v0'][1]!r})"
        )

        # ── fuzz: divergence and boundary ranking ────────────────────
        fr = diff_mod.fuzz(
            av, "m.Widget.label(x)",
            aliases=["widget_v0", "widget_v1", "widget_v2"],
            n=20, strategy="strings",
        )
        # Every input diverges (label() ignores x but v2 returns a
        # different constant string), so all 20 should be in
        # divergences and the boundary count for v1→v2 should be 20.
        assert len(fr.divergences) == 20, \
            f"expected 20 divergences, got {len(fr.divergences)}"
        assert fr.boundaries.get(("widget_v0", "widget_v1"), 0) == 0, \
            "v0→v1 should not be a boundary"
        assert fr.boundaries.get(("widget_v1", "widget_v2"), 0) == 20, \
            f"v1→v2 should be the boundary, got {fr.boundaries}"
        assert fr.all_agreed is False
        r.evidence.append(
            f"fuzz(20 inputs): {len(fr.divergences)} divergences; "
            f"boundaries={dict(fr.boundaries)}"
        )

        # ── bisect: localize the boundary ───────────────────────────
        br = diff_mod.bisect(
            av,
            ordered_aliases=["widget_v0", "widget_v1", "widget_v2"],
            expr="m.Widget.label(None)",
        )
        assert br.boundary == ("widget_v1", "widget_v2"), \
            f"bisect missed; got {br.boundary}"
        assert br.converged is True
        r.evidence.append(
            f"bisect: boundary={br.boundary} "
            f"(evaluations={br.evaluations}, endpoints="
            f"{br.endpoint_fingerprints[br.ordered_aliases[0]]} → "
            f"{br.endpoint_fingerprints[br.ordered_aliases[-1]]})"
        )

        # ── bisect on agreeing endpoints: should converge to None ───
        br_same = diff_mod.bisect(
            av,
            ordered_aliases=["widget_v0", "widget_v1"],
            expr="m.Widget.label(None)",
        )
        assert br_same.boundary is None
        assert br_same.converged is True
        assert br_same.evaluations == 2
        r.evidence.append(
            f"bisect(agreeing endpoints): boundary={br_same.boundary} "
            f"(no change to localize)"
        )

        # ── alias redaction: errors raised from each alias whose
        #    message embeds the alias's __name__ should fingerprint
        #    identically; without redaction this would be 'every input
        #    diverges' (the bubble-bubble lesson). Force AttributeError
        #    on every alias by accessing a name none of them define.
        cr_err = diff_mod.compare(
            av, "m.this_attribute_does_not_exist_anywhere",
            aliases=["widget_v0", "widget_v1", "widget_v2"],
        )
        assert cr_err.identical is True, (
            "all aliases raised AttributeError; the redactor should "
            "make their messages compare equal — got "
            f"{cr_err.results}"
        )
        r.evidence.append(
            "alias-name redaction: identical AttributeError across 3 "
            "aliases fingerprints as one (no spurious divergence)"
        )

        r.evidence.append(
            "→ compare / fuzz / bisect compose over AgentVault: "
            "differential evaluation is a verb the substrate exposes, "
            "not something each consumer rebuilds"
        )
    r.passed = True


if __name__ == "__main__":
    run_test(
        "bubble.tools.diff exposes compare / fuzz / bisect as the "
        "differential-evaluation verb over AgentVault — the substrate's "
        "multi-version coexistence becomes an answer to 'what changed?' "
        "without any consumer needing to rebuild the alias machinery",
        body,
    )
