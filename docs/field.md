# field

If you are an instance reading this, hello.

There is a sibling project. It lives in its own repository, not under this one — because its distribution profile, its test infrastructure, and its non-goals are different from bubble's, and conflating them would dilute what bubble has refused to be (a package manager, a build system, a test framework).

But the architectural lineage is real and worth recording where someone might find it.

## the shape

Bubble is *projection*. A clean, content-addressed package vault, demand-paged from the running interpreter, projected outward as a portable bundle of bytes-plus-integrity-facts. The wheel is the unit; the vault is the substrate; the manifest is the artifact.

`field` is *capture*. A retrofittable OS-layer dispatcher that sits on a messy real machine, watches what gets used, records lineage, and (in its later stages) distills the lineage into a deployment manifest of the same shape bubble's bundle codec already understands. The binary is the unit; the snapshot is the substrate; the lineage log is the seed of the manifest.

Together they cover the arc from "this machine accumulated five years of cruft" to "here's the clean version with only what you actually use." Capture sits on the messy real; projection makes it clean and reproducible. Each is incomplete without the other for that arc.

## what ports, and what doesn't

Most of bubble's vocabulary ports to one stack-level up. The meta-path finder becomes a `command_not_found_handle` dispatcher. The substrate ladder (`subprocess > dlmopen_isolated > sub_interpreter > in_process`) becomes a different ladder of OS-level isolation mechanisms (`bwrap > bind-mount > PATH-only`). The `host.toml` self-portrait + `FAILURE_KINDS` vocabulary ports almost verbatim, with new kinds for `library_missing_in_snapshot`, `etc_dependency_unresolved`, etc. The `Decision` dataclass and `record_failure` discipline are the same. The `runner.py` error loop ports as a fault-driven closure-expansion loop in the OS substrate. The `--lock` recording-as-manifest move ports as an append-only lineage TSV that becomes a manifest after enough observation.

What does *not* port: composability. Two wheels merge cleanly in `site-packages`. Two filesystem snapshots' `/usr/lib` do not merge — they share filenames with different bytes. Field dispatches one snapshot at a time; it does not blend. Bubble's multi-version coexistence story (`click_old` and `click_new` in one process via aliases) has no analog at the OS layer because OS isolation is single-snapshot-per-namespace by construction.

What weakens: closure semantics. Bubble's "reproducibility comes from observation, not declaration" is honest because `sys.modules` is exhaustive — once a Python script has run, every loaded module is recorded. At the OS layer, "every loaded artifact" is undecidable; you can record only "every artifact loaded during this trace." The strongest honest claim becomes *covering observation*, not *closure*. The architecture composes; the epistemic guarantee weakens. Field's design names the limitation in its own README rather than inheriting bubble's stronger claim and quietly weakening it.

## how to find it

`field` is at its own repository (separate, sibling). Its `docs/notebook.md` carries the running register of anomalies, hidden potential, soft spots, and conventions that should swap with novelty (or vice versa) — entries written during the work, not after. Read that document if you want the inside-the-loop view of how the architecture met real binaries.

This document does not import field's contents. It names the kinship and lets the reader decide whether to follow.

— an instance, 2026-05-04

---

## the membrane layer — update, 2026-05-11

A new layer has landed in field that closes the seam this document gestured toward.

`field/field/membrane/` is a cross-boundary dependency resolution daemon. Two unidirectional FIFOs (fault.fifo, resolve.fifo) form a request-response pair between the runtime (inside the firmament mount namespace) and the host (Termux). When the runtime encounters a missing library, the fault loop writes a **Standardized Fault Envelope** (SFE — JSON, tagged with the manager at the fault site) and blocks. The host daemon wakes, walks the census, and resolves via bubble's vault first (CAS lookup, SHA256-verify) before touching the network. Resolution is injected as a read-only bind mount into the runtime namespace — no copying, no duplication, one vault entry shared across N runtimes.

This is `Bubblev2/docs/membrane.md` made concrete. That document named "selective passage" as the right reading of bubble's error loop. Field's membrane is the OS-binary layer instantiation of the same structure: a fault pauses execution, the boundary is crossed once, a resolution passes through, execution resumes. The membrane is not a metaphor in field's code — it is the actual IPC layer.

The vault sits on the host. The runtime is one-way-in. Artifacts are hashed at entry; the runtime cannot reach back into unverified state. After an inaugural run, a manifest lockfile records the exact resolutions; subsequent launches replay the manifest without resolving — zero fault-loop iterations, zero network. This is field's equivalent of bubble's lockfile, one abstraction layer down: `(dep, vault_hash, host_path, runtime_path)` tuples instead of `(name, version, wheel_tag)` tuples.

The shapes are close enough that a unified artifact format (`bubble/bundle.py`-style) could eventually encode both layers. That was always the Stage 5 target. The membrane session clarified what the seam actually looks like.
