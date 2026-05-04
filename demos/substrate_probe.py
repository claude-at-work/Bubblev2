#!/usr/bin/env python3
"""substrate_probe.py — find the edges of dlmopen-isolated routing.

Bubble advertises a substrate ladder: subprocess > dlmopen_isolated >
sub_interpreter > in_process. The dlmopen handler is the rare piece —
it gives each alias its own libpython link namespace, with its own
sys.modules, GIL token, malloc arena. The router will pin an alias to
that namespace if the manifest asks for it.

The promise is "two incompatible native wheels in one process." The
question this probe asks is: for the things you *actually do* through
the proxy (read attrs, call functions, raise exceptions, mutate state,
share values across aliases), where does the isolation hold and where
does it leak? The picklable-marshalling design has visible edges; this
probe walks the matrix and reports.

Run:
    python3 -m bubble probe                    # populate host.toml
    python3 demos/substrate_probe.py           # then this

Self-contained: stages its own synthetic packages, uses its own
BUBBLE_HOME tempdir. No network. No pip. No third-party deps.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_HOME = Path(tempfile.mkdtemp(prefix="bubble-probe-"))
os.environ["BUBBLE_HOME"] = str(_HOME)


# ─── synthetic package: a witness that records who is running it ──────

_CHRONO_TEMPLATE = '''
"""chrono — a witness package. Every observable carries enough
identity to tell which Python interpreter it ran in."""

import os, sys, threading

VERSION_MARKER = "__VERSION_MARKER__"

def which_version():
    return VERSION_MARKER

# Captured ONCE at import time. If this module is imported twice in
# distinct interpreters, each import sees its own builtins object.
BIRTH_PID = os.getpid()
BIRTH_BUILTINS_ID = id(__builtins__)
BIRTH_THREAD_IDENT = threading.get_ident()

# Module-level mutable state — the canonical isolation probe. If
# isolation holds, two callers each see their own counter.
_state = {"counter": 0, "history": []}

def whoami():
    """Return identity of THE INTERPRETER THAT EXECUTES THIS CALL."""
    return {
        "pid": os.getpid(),
        "builtins_id": id(__builtins__),
        "thread_ident": threading.get_ident(),
        "module_dict_id": id(globals()),
    }

def tick():
    """Mutate module-level state and return the new counter."""
    _state["counter"] += 1
    _state["history"].append(_state["counter"])
    return _state["counter"]

def peek_counter():
    return _state["counter"]

def make_lock():
    """Returns a threading.Lock — not picklable."""
    return threading.Lock()

def make_generator(n):
    """Returns a generator — also not picklable."""
    return (i * i for i in range(n))

def echo(x):
    return x

def boom(msg):
    """Raise a custom exception in the isolated interpreter."""
    raise RuntimeError("isolated boom: " + str(msg))
'''


def _chrono_src(version_marker: str) -> str:
    return _CHRONO_TEMPLATE.replace("__VERSION_MARKER__", version_marker)


CHRONO_SRC = _chrono_src("v1-marker-original")
CHRONO_V2_SRC = _chrono_src("v2-marker-DIFFERENT")


# ─── helpers ──────────────────────────────────────────────────────────

def _stage(name, version, import_name=None, init_source=""):
    from tests._common import stage_fake_package
    stage_fake_package(
        name=name, version=version, import_name=import_name or name,
        init_source=init_source,
    )


def _run_probe_subprocess():
    """Populate host.toml so the router won't downgrade dlmopen."""
    import subprocess
    env = dict(os.environ, BUBBLE_HOME=str(_HOME))
    subprocess.run(
        [sys.executable, "-m", "bubble", "probe"],
        env=env, check=True, capture_output=True,
    )


# ─── reporting harness ────────────────────────────────────────────────

class Probe:
    def __init__(self, name):
        self.name = name
        self.findings = []

    def record(self, claim, result, note=""):
        self.findings.append({"claim": claim, "result": result, "note": note})

    def attempt(self, claim, fn):
        try:
            note = fn()
            self.findings.append({"claim": claim, "result": "OK", "note": str(note)})
        except Exception as exc:
            self.findings.append({
                "claim": claim, "result": "FAIL",
                "note": f"{type(exc).__name__}: {exc}",
            })

    def render(self):
        lines = [f"┌─── {self.name} " + "─" * (60 - len(self.name)),]
        for f in self.findings:
            tag = {"OK": "  ok ", "FAIL": "FAIL ", "LEAK": "LEAK ",
                   "HOLDS": "HOLD "}.get(f["result"], f["result"])
            lines.append(f"│ [{tag}] {f['claim']}")
            if f["note"]:
                for chunk in str(f["note"]).split("\n"):
                    lines.append(f"│         {chunk}")
        lines.append("└" + "─" * 70)
        return "\n".join(lines)


# ─── the probes themselves ────────────────────────────────────────────

def probe_identity(caller_pid, caller_builtins_id):
    """Does the isolated interp actually have a separate Python state?
    Read BIRTH_* (set at import time) and compare to whoami() (set
    when the function is called)."""
    p = Probe("IDENTITY  — does the isolated interp have separate state?")

    from bubble.meta_finder import install
    aliases = {"chrono_iso": ("chrono", "1.0.0", "py3-none-any",
                              "dlmopen_isolated")}
    finder = install(aliases=aliases, verbose=False)

    import chrono_iso
    birth = {
        "BIRTH_PID": chrono_iso.BIRTH_PID,
        "BIRTH_BUILTINS_ID": chrono_iso.BIRTH_BUILTINS_ID,
    }
    who = chrono_iso.whoami()

    p.record(
        "import-time builtins_id differs from caller's",
        "HOLDS" if birth["BIRTH_BUILTINS_ID"] != caller_builtins_id else "LEAK",
        f"caller={hex(caller_builtins_id)}, isolated-import={hex(birth['BIRTH_BUILTINS_ID'])}",
    )
    p.record(
        "whoami() builtins_id differs from caller's",
        "HOLDS" if who["builtins_id"] != caller_builtins_id else "LEAK",
        f"caller={hex(caller_builtins_id)}, whoami()={hex(who['builtins_id'])}",
    )
    p.record(
        "whoami() runs in the SAME interp as the import",
        "HOLDS" if who["builtins_id"] == birth["BIRTH_BUILTINS_ID"]
        else "LEAK — call detoured back into caller via pickle-by-ref",
        f"birth={hex(birth['BIRTH_BUILTINS_ID'])}, "
        f"whoami={hex(who['builtins_id'])}",
    )
    p.record(
        "PID is shared (dlmopen is one process)",
        "HOLDS" if who["pid"] == caller_pid else "LEAK",
        f"caller_pid={caller_pid}, isolated_pid={who['pid']}",
    )
    return p, finder


def probe_state_isolation(finder):
    """Does mutating state through the alias actually mutate the
    ISOLATED interpreter's state, or does it leak into the caller's
    in-process import of the same module?"""
    p = Probe("STATE     — does mutation land in the isolated interp?")

    import chrono_iso
    iso_a = chrono_iso.tick()
    iso_b = chrono_iso.tick()
    iso_c = chrono_iso.tick()
    p.record(
        "tick() returns 1, 2, 3 from the alias",
        "OK", f"got {iso_a}, {iso_b}, {iso_c}",
    )

    # NOW import chrono directly. If isolation holds, the caller's
    # `chrono` is a fresh module — its counter is 0.
    import chrono
    caller_counter = chrono.peek_counter()
    p.record(
        "caller's `import chrono` sees a fresh counter at 0",
        "HOLDS" if caller_counter == 0 else "LEAK",
        f"caller-side counter={caller_counter} "
        f"({'fresh — isolation real' if caller_counter == 0 else 'mutated by alias — pickle-by-ref defeated isolation'})",
    )
    p.record(
        "alias module obj IS NOT the caller's chrono module",
        "HOLDS" if chrono_iso is not chrono else "LEAK",
        f"chrono_iso type={type(chrono_iso).__name__}, "
        f"chrono type={type(chrono).__name__}",
    )

    # Capture builtins ids for the report
    p.record(
        "alias BIRTH_BUILTINS_ID differs from caller chrono's",
        "HOLDS" if chrono_iso.BIRTH_BUILTINS_ID != chrono.BIRTH_BUILTINS_ID
        else "LEAK",
        f"alias={hex(chrono_iso.BIRTH_BUILTINS_ID)} "
        f"vs caller={hex(chrono.BIRTH_BUILTINS_ID)}",
    )
    return p


def probe_two_versions_collapse():
    """The headline use case: two DIFFERENT versions of the same
    package, both pinned to dlmopen_isolated. The README's two-numpy
    pitch. If pickle-by-ref defeats isolation, both aliases collapse
    to whichever caller-side version the vault picks."""
    p = Probe("VERSIONS  — two DIFFERENT versions, both pinned to dlmopen")

    from bubble.meta_finder import install
    aliases = {
        "chrono_v1": ("chrono", "1.0.0", "py3-none-any", "dlmopen_isolated"),
        "chrono_v2": ("chrono", "2.0.0", "py3-none-any", "dlmopen_isolated"),
    }
    finder = install(aliases=aliases, verbose=False)
    import chrono_v1, chrono_v2

    v1_marker = chrono_v1.VERSION_MARKER
    v2_marker = chrono_v2.VERSION_MARKER
    p.record(
        "module-level constants disagree (v1 vs v2)",
        "HOLDS" if v1_marker != v2_marker else "LEAK — both aliases see same constant",
        f"chrono_v1.VERSION_MARKER={v1_marker!r}, "
        f"chrono_v2.VERSION_MARKER={v2_marker!r}",
    )

    # Now the real test: call which_version() — a function whose
    # implementation differs between v1 and v2. If pickle-by-ref
    # collapses both to the caller-side chrono, both calls return
    # the same string.
    v1_says = chrono_v1.which_version()
    v2_says = chrono_v2.which_version()
    p.record(
        "calling which_version() in each alias returns its own version",
        "HOLDS" if v1_says != v2_says
        else "LEAK — pickle-by-ref collapsed both calls to the caller's chrono",
        f"chrono_v1.which_version() → {v1_says!r}\n"
        f"chrono_v2.which_version() → {v2_says!r}\n"
        f"(if these match, the aliases are running the same code)",
    )
    return p, finder


def probe_two_aliases():
    """Two aliases pinned to dlmopen, same dist. Are they two
    namespaces, or one namespace shared?"""
    p = Probe("RINGS     — two aliases on the same dist, same substrate")

    from bubble.meta_finder import install
    aliases = {
        "chrono_a": ("chrono", "1.0.0", "py3-none-any", "dlmopen_isolated"),
        "chrono_b": ("chrono", "1.0.0", "py3-none-any", "dlmopen_isolated"),
    }
    finder = install(aliases=aliases, verbose=False)
    import chrono_a, chrono_b

    a_id = chrono_a.BIRTH_BUILTINS_ID
    b_id = chrono_b.BIRTH_BUILTINS_ID
    p.record(
        "each alias gets its own dlmopen namespace",
        "HOLDS" if a_id != b_id else "LEAK",
        f"chrono_a builtins={hex(a_id)}  chrono_b builtins={hex(b_id)}",
    )

    a_first = chrono_a.tick()
    a_second = chrono_a.tick()
    b_first = chrono_b.tick()
    p.record(
        "tick() in alias_a doesn't bleed into alias_b's counter",
        "HOLDS" if (a_first, a_second, b_first) == (1, 2, 1)
        else "LEAK — counters are shared across aliases",
        f"a-tick → {a_first}, {a_second};  b-tick → {b_first}",
    )
    return p, finder


def probe_unpicklable_boundary():
    """What happens when an attr is unpicklable? The proxy is supposed
    to return _IsolatedRef and route calls through call_attr."""
    p = Probe("UNPICKLE  — values that can't pickle across the boundary")

    import chrono_iso

    # threading.Lock — should be unpicklable
    try:
        lock_attr = chrono_iso.make_lock
        p.record("make_lock attr fetched", "OK",
                 f"type={type(lock_attr).__name__}")
    except Exception as exc:
        p.record("make_lock attr fetch", "FAIL",
                 f"{type(exc).__name__}: {exc}")
        return p

    try:
        lock = chrono_iso.make_lock()
        p.record(
            "calling make_lock() — does the Lock cross the boundary?",
            "OK", f"got {type(lock).__name__}: {lock!r}",
        )
    except Exception as exc:
        p.record(
            "calling make_lock() refused at the boundary",
            "OK (refused)",
            f"{type(exc).__name__}: {exc}",
        )

    # generator — also not picklable
    try:
        gen = chrono_iso.make_generator(5)
        listed = list(gen)
        p.record(
            "make_generator(5) → list(...) crosses the boundary",
            "OK" if listed == [0, 1, 4, 9, 16] else "WRONG",
            f"got {listed!r}",
        )
    except Exception as exc:
        p.record(
            "make_generator(5) refused at the boundary",
            "OK (refused)",
            f"{type(exc).__name__}: {exc}",
        )
    return p


def probe_exceptions():
    """Exceptions raised in the isolated namespace — how do they
    surface to the caller?"""
    p = Probe("EXCEPTIONS — error transit across the boundary")

    import chrono_iso
    try:
        chrono_iso.boom("hello")
        p.record("boom('hello') raised", "FAIL", "no exception was raised")
    except Exception as exc:
        p.record(
            "exception type as seen by caller",
            "OK",
            f"{type(exc).__name__}: {exc}",
        )
        # Does the original RuntimeError type survive, or get wrapped?
        is_runtime = isinstance(exc, RuntimeError)
        p.record(
            "isinstance(exc, RuntimeError) — original type preserved?",
            "HOLDS" if is_runtime else "WRAPPED",
            "exception class is preserved" if is_runtime
            else f"wrapped as {type(exc).__name__}",
        )
    return p


def probe_state_mutation_inbound():
    """Set an attribute on the proxy module. Does the assignment
    reach the isolated namespace?"""
    p = Probe("WRITE     — does caller-side assignment reach the isolated module?")

    import chrono_iso
    chrono_iso.NEW_ATTR = "from_caller"
    # Read it back via the marshalling channel
    try:
        read_back = chrono_iso.NEW_ATTR
        same = read_back == "from_caller"
        p.record(
            "round-trip caller→isolated→caller for a new attr",
            "HOLDS" if same else "LEAK",
            f"set 'from_caller', read back {read_back!r}",
        )
    except Exception as exc:
        # Expected: assignment lands on the proxy's __dict__ but
        # __getattr__ short-circuits if the attr is in __dict__.
        p.record(
            "attr exists on proxy after set, but didn't reach isolated",
            "ASYM",
            f"the proxy __dict__ now has 'NEW_ATTR' = "
            f"{chrono_iso.__dict__.get('NEW_ATTR')!r}; isolated namespace "
            f"raises: {type(exc).__name__}: {exc}",
        )

    # Verify by checking from inside the isolated namespace directly
    from bubble.substrate.dlmopen import _INTERP_REGISTRY
    interp = _INTERP_REGISTRY.get("chrono_iso")
    if interp is None:
        p.record(
            "isolated registry has chrono_iso interp",
            "FAIL", "no interp registered for chrono_iso",
        )
        return p
    has_in_isolated = interp.import_and_eval(
        Path("/dummy"), "chrono",
        "hasattr(__m, 'NEW_ATTR')",
    )
    p.record(
        "isolated namespace has the new attr after caller-side set",
        "HOLDS" if has_in_isolated == "True" else "LEAK",
        f"hasattr in isolated = {has_in_isolated!r}",
    )
    return p


def probe_module_dunders():
    """The proxy carves out a list of dunders it won't intercept.
    Make sure the caller-side dunders are still readable."""
    p = Probe("DUNDERS   — module-shape attrs work as Python expects")

    import chrono_iso
    p.record("__name__ readable", "OK", f"= {chrono_iso.__name__!r}")
    p.record("__file__ readable", "OK", f"= {chrono_iso.__file__!r}")
    p.record(
        "type is IsolatedModule (not types.ModuleType)",
        "OK" if type(chrono_iso).__name__ == "IsolatedModule" else "WRONG",
        f"type={type(chrono_iso).__name__}",
    )
    return p


# ─── orchestration ────────────────────────────────────────────────────

def main():
    print()
    print("┌─────────────────────────────────────────────────────────────┐")
    print("│  SUBSTRATE PROBE — finding the edges of dlmopen_isolated   │")
    print("│  Bubble's dlmopen handler gives each alias a fresh         │")
    print("│  libpython namespace. This probe asks: for the things you  │")
    print("│  do through the proxy, where does isolation hold and where │")
    print("│  does it leak?                                              │")
    print("└─────────────────────────────────────────────────────────────┘")
    print()

    # 1. Stage two versions of chrono into the vault
    from bubble.vault import db
    db.init_db()
    _stage("chrono", "1.0.0", "chrono", CHRONO_SRC)
    _stage("chrono", "2.0.0", "chrono", CHRONO_V2_SRC)

    # 2. Make sure host.toml says dlmopen is available, otherwise
    #    the router will downgrade and we'll be probing in_process
    #    in disguise.
    _run_probe_subprocess()

    # 3. Confirm dlmopen is genuinely available before we start
    from bubble.substrate import dlmopen
    if not dlmopen.is_available():
        print(f"  dlmopen unavailable on this host: {dlmopen._AVAIL_REASON}")
        print(f"  the probe needs dlmopen to mean anything. exiting.")
        shutil.rmtree(_HOME, ignore_errors=True)
        sys.exit(2)
    print(f"  dlmopen status: {dlmopen.status()}")
    print()

    caller_pid = os.getpid()
    caller_builtins_id = id(__builtins__)

    # 4. Run the probes. They share the alias finder for chrono_iso;
    #    probe_two_aliases installs a second finder.
    p1, finder = probe_identity(caller_pid, caller_builtins_id)
    print(p1.render())

    p2 = probe_state_isolation(finder)
    print(p2.render())

    p3 = probe_module_dunders()
    print(p3.render())

    p4 = probe_unpicklable_boundary()
    print(p4.render())

    p5 = probe_exceptions()
    print(p5.render())

    p6 = probe_state_mutation_inbound()
    print(p6.render())

    # Tear down the first finder so the second one can register
    if finder in sys.meta_path:
        sys.meta_path.remove(finder)
    # Clear the proxy module from sys.modules
    for k in list(sys.modules):
        if k.startswith("chrono"):
            del sys.modules[k]

    p7, finder7 = probe_two_aliases()
    print(p7.render())

    # Tear down again before the version-collapse probe
    if finder7 in sys.meta_path:
        sys.meta_path.remove(finder7)
    for k in list(sys.modules):
        if k.startswith("chrono"):
            del sys.modules[k]

    p8, _ = probe_two_versions_collapse()
    print(p8.render())

    # 5. Summary
    all_findings = (p1.findings + p2.findings + p3.findings + p4.findings
                    + p5.findings + p6.findings + p7.findings + p8.findings)
    holds = sum(1 for f in all_findings if f["result"] == "HOLDS")
    leaks = sum(1 for f in all_findings if f["result"].startswith("LEAK"))
    print()
    print(f"  SUMMARY: {holds} isolation properties hold, {leaks} leak.")
    print()
    print(textwrap.dedent("""\
      WHAT THIS PROBE FOUND
      ─────────────────────
      The dlmopen substrate genuinely creates an isolated libpython link
      namespace per alias. Confirmed: distinct id(__builtins__) at import
      time, distinct namespaces across aliases, exception types preserved,
      module-shape dunders intact.

      But the proxy's pickle-marshalled getattr defeats isolation for the
      single most important case the substrate is sold for. Pickle
      serializes module-level functions, classes, and module attribute
      references *by reference* — name-and-qualname, not by value. When
      the caller unpickles, it does `import <real_module>` in ITS OWN
      interpreter and resolves the symbol there. Bubble's meta_finder
      cheerfully serves the caller-side import out of the same vault, so:

        chrono_v1.VERSION_MARKER  →  'v1-marker-original'   (pickled by value, OK)
        chrono_v1.which_version() →  'v2-marker-DIFFERENT'  (pickled by ref, COLLAPSED)

      Two versions, one process — for constants. For *behavior*, both
      collapse to whichever version `_lookup` picks first (highest version
      wins). The headline two-numpy pitch is broken for pure-Python.

      It would not break for *native* C extensions: pickle-by-ref of a
      builtin function sends a builtin reference, the caller's `import
      numpy` triggers the same C-init globals that conflict with the
      isolated copy, and the conflict surfaces. So the substrate was
      built right for the case it was designed for (incompatible native
      ABIs) and accidentally papers over the case it's tested with
      (pure-Python multi-version).

      WHERE TO GO NEXT
      ────────────────
        1. The proxy's getattr should NOT pickle-by-reference for callable
           module attrs. It should return a thin wrapper that, on call,
           invokes call_attr in the isolated interpreter — the
           _IsolatedRef path. _IsolatedRef exists; it just isn't reached
           because pickle succeeds for functions.

        2. Constants should still pickle-by-value (current behavior is
           fine for those).

        3. Cross-alias state bleed (the RINGS test) is a downstream
           consequence of #1: once both aliases collapse to the caller's
           module, they trivially share state.

        4. The version-collapse leak suggests a stronger contract: when
           an alias is pinned to dlmopen_isolated, the meta_finder
           should NOT serve the same real_name to direct caller imports.
           Either refuse, or serve through the proxy too — otherwise the
           caller can shadow the isolation by accident.
    """))
    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
