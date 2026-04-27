#!/usr/bin/env python3
"""impossible.py — Three things you CANNOT do without Bubble.

This script stages two versions of a library into the vault, then loads
both into a single Python process and runs the same input through each.
Standard Python can't do this. With pip, one process gets one version.
With Bubble's alias system, one process gets both — and the comparison
is an in-memory diff, not a subprocess + serialization dance.

Run:
    python3 demos/impossible.py

Requires: the bubble package importable (e.g. pip install -e . or
the pyz on your PATH). No network access. No third-party packages.
Creates and tears down its own vault in a tempdir.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from pathlib import Path

# Ensure bubble is importable when running from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ─── vault bootstrap ────────────────────────────────────────────────

# Isolate: set BUBBLE_HOME BEFORE importing bubble.* so config picks
# up our tempdir instead of the user's real ~/.bubble.
_HOME = Path(tempfile.mkdtemp(prefix="bubble-impossible-"))
os.environ["BUBBLE_HOME"] = str(_HOME)


def _stage_package(name, version, wheel_tag, import_name, init_source,
                   top_level_imports, submodules=None, flat=False,
                   contention_import=None):
    """Build a synthetic package and commit it to the vault.

    `contention_import` is a name written to top_level.txt that this
    package doesn't actually provide — it creates a phantom claim that
    a different package also claims, triggering contention logging.
    """
    from bubble.vault import store, db

    staged = store.stage_dir()
    dist_info = staged / f"{name}-{version}.dist-info"

    if flat:
        (staged / f"{import_name}.py").write_text(
            textwrap.dedent(init_source).lstrip()
        )
    else:
        pkg_dir = staged / import_name
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(
            textwrap.dedent(init_source).lstrip()
        )
        for sub_name, sub_src in (submodules or {}).items():
            (pkg_dir / f"{sub_name}.py").write_text(
                textwrap.dedent(sub_src).lstrip()
            )

    dist_info.mkdir()
    metadata = "\n".join([
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
    ]) + "\n\n"
    (dist_info / "METADATA").write_text(metadata)
    (dist_info / "WHEEL").write_text(
        f"Wheel-Version: 1.0\nGenerator: impossible-demo\n"
        f"Root-Is-Purelib: true\nTag: {wheel_tag}\n"
    )
    tl_names = list(top_level_imports)
    if contention_import:
        tl_names.append(contention_import)
    (dist_info / "top_level.txt").write_text("\n".join(tl_names) + "\n")

    py_tag, abi_tag, plat_tag = (wheel_tag.split("-") + ["py3", "none", "any"])[:3]
    store.commit(
        name=name, version=version, wheel_tag=wheel_tag,
        python_tag=py_tag, abi_tag=abi_tag, platform_tag=plat_tag,
        staged=staged, source="impossible-demo",
    )
    return name, version, wheel_tag


# ─── demo library source ─────────────────────────────────────────────

CIPHER_V1_INIT = '''\
"""
cipher v1 — simple substitution ciphers.
A real library you might upgrade. v1 has known quirks.
"""
SHIFT_MAP = {
    "a": "n", "b": "o", "c": "p", "d": "q", "e": "r",
    "f": "s", "g": "t", "h": "u", "i": "v", "j": "w",
    "k": "x", "l": "y", "m": "z", "n": "a", "o": "b",
    "p": "c", "q": "d", "r": "e", "s": "f", "t": "g",
    "u": "h", "v": "i", "w": "j", "x": "k", "y": "l",
    "z": "m",
}

def encode(text):
    """ROT13 — but only alphabetic chars, and it lowercases input."""
    result = []
    for ch in text.lower():
        if ch in SHIFT_MAP:
            result.append(SHIFT_MAP[ch])
        elif ch == " ":
            result.append(" ")
        else:
            # BUG: non-alpha chars are silently dropped
            pass
    return "".join(result)

def decode(text):
    """ROT13 is its own inverse."""
    return encode(text)

def info():
    return {"version": "1.0.0", "method": "rot13", "flaw": "drops non-alpha, lowercases"}
'''

CIPHER_V2_INIT = '''\
"""
cipher v2 — improved substitution ciphers.
v2 fixes v1's bugs and adds new capabilities.
"""
SHIFT_MAP = {
    "a": "n", "b": "o", "c": "p", "d": "q", "e": "r",
    "f": "s", "g": "t", "h": "u", "i": "v", "j": "w",
    "k": "x", "l": "y", "m": "z", "n": "a", "o": "b",
    "p": "c", "q": "d", "r": "e", "s": "f", "t": "g",
    "u": "h", "v": "i", "w": "j", "x": "k", "y": "l",
    "z": "m",
}

def encode(text):
    """ROT13 preserving case, punctuation, and digits."""
    result = []
    for ch in text:
        if ch.lower() in SHIFT_MAP:
            shifted = SHIFT_MAP[ch.lower()]
            result.append(shifted.upper() if ch.isupper() else shifted)
        else:
            # FIX: non-alpha chars pass through
            result.append(ch)
    return "".join(result)

def decode(text):
    """ROT13 is its own inverse."""
    return encode(text)

def hash_text(text):
    """New in v2: simple hash for integrity checking."""
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def info():
    return {"version": "2.0.0", "method": "rot13+", "fix": "preserves case and punctuation"}
'''

# A completely different package that claims the same import name
# — simulates the real opencv-python / opencv-python-headless conflict.
CRYPTO_V1_INIT = '''\
"""
cryptolib — a different package that ALSO claims the 'cipher' import name.
pip silently lets the last install win. Bubble records the contention.
"""
def encode(text):
    """XOR cipher with a fixed key. Not production-grade. Demonstrates contention."""
    key = 42
    return "".join(chr(ord(ch) ^ key) for ch in text)

def decode(text):
    return encode(text)  # XOR is its own inverse

def info():
    return {"package": "cryptolib", "version": "1.0.0", "method": "xor-42"}
'''

FORMATTTER_V1_INIT = '''\
"""
formatter — a text rendering library. Shows cross-library composition:
cipher produces encoded text, formatter renders it.
"""
def banner(text, width=60):
    border = "=" * width
    centered = text.center(width)
    return f"{border}\\n{centered}\\n{border}"

def table(rows, headers=None):
    """Render a simple ASCII table."""
    if headers:
        rows = [headers] + list(rows)
    if not rows:
        return "(empty)"
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for i, row in enumerate(rows):
        line = " | ".join(str(v).ljust(w) for v, w in zip(row, widths))
        lines.append(line)
        if i == 0 and headers:
            lines.append("-+-".join("-" * w for w in widths))
    return "\\n".join(lines)
'''


# ─── demonstrations ─────────────────────────────────────────────────

def demo_multiversion():
    """IMPOSSIBLE THING #1: Two versions of the same library, one process.

    In standard Python, `import cipher` gives you exactly one version.
    The diamond dependency problem: Library A wants cipher 1, Library B
    wants cipher 2. pip resolves to one. The other loses.

    Bubble's alias system breaks the diamond: both versions coexist.
    The comparison is a simple dict diff — no subprocess, no serialization.
    """
    from bubble.meta_finder import install

    # The vault already has cipher 1.0 and 2.0 from setup.
    # The alias map tells VaultFinder how to resolve two synthetic
    # import names to two different vault entries of the same package.
    aliases = {
        "cipher_old": ("cipher", "1.0.0", "py3-none-any"),
        "cipher_new": ("cipher", "2.0.0", "py3-none-any"),
    }

    finder = install(aliases=aliases, verbose=False)

    # Remove from sys.modules after demo
    loaded_before = set(sys.modules)

    import cipher_old  # noqa: E402  — resolves to cipher 1.0.0
    import cipher_new  # noqa: E402  — resolves to cipher 2.0.0

    test_inputs = [
        "Hello World",
        "Python 3.13!",
        "BUBBLE_VAULT",
        "the quick brown fox",
    ]

    results = []
    for text in test_inputs:
        old_out = cipher_old.encode(text)
        new_out = cipher_new.encode(text)
        results.append({
            "input": text,
            "v1": old_out,
            "v2": new_out,
            "match": old_out == new_out,
            "v1_info": cipher_old.info(),
            "v2_info": cipher_new.info(),
        })

    # Clean up sys.modules
    for mod in list(sys.modules):
        if mod not in loaded_before:
            del sys.modules[mod]
    if finder in sys.meta_path:
        sys.meta_path.remove(finder)

    return results


def demo_contention():
    """IMPOSSIBLE THING #2: Contention audit.

    Two different PyPI packages claim the same import name.
    pip: last install wins, silently. You never know it happened.

    Bubble: the contention is recorded in the vault index.
    store.top_level_contentions makes it auditable.

    This is the cv2 / opencv-python / opencv-python-headless problem,
    but it can happen with any namespace collision.
    """
    from bubble.vault import store

    contentions = store.top_level_contentions
    return [
        {
            "import_name": c["import_name"],
            "incoming": c["incoming"],
            "existing": c["existing"],
        }
        for c in contentions
    ]


def demo_vault_archaeology():
    """IMPOSSIBLE THING #3: Content-addressed archaeology.

    The vault stores packages by (name, version, wheel_tag). Every
    import name is indexed with a content hash. You can query the
    vault to answer questions pip cannot:

    - What import names does the vault serve?
    - Which packages compete for the same import name?
    - What's the content hash of each import subtree?
    - Which version of each package is newest?

    pip knows dependencies. It does not know content hashes or
    import-name collisions. The vault does.
    """
    from bubble.vault import db

    conn = db.connect()
    import_names = list(conn.execute(
        "SELECT import_name, COUNT(*) as cnt FROM top_level "
        "GROUP BY import_name ORDER BY cnt DESC, import_name"
    ).fetchall())

    packages = list(conn.execute(
        "SELECT name, version, wheel_tag, source FROM packages "
        "ORDER BY name, version"
    ).fetchall())

    content_hashes = list(conn.execute(
        "SELECT import_name, package, version, import_sha256 FROM top_level "
        "ORDER BY import_name"
    ).fetchall())

    conn.close()

    return {
        "import_names": [
            {"name": row[0], "claimed_by": row[1]} for row in import_names
        ],
        "packages": [
            {"name": row[0], "version": row[1], "tag": row[2], "source": row[3]}
            for row in packages
        ],
        "content_hashes": [
            {"import": row[0], "package": row[1], "version": row[2], "sha256": row[3]}
            for row in content_hashes
        ],
    }


def demo_cross_library_synthesis(v1_results, v2_results):
    """IMPOSSIBLE THING #4 (consequence): Cross-version diffing with
    a rendering library that has NO dependency on the versioned library.

    formatter doesn't know about cipher. cipher doesn't know about
    formatter. But we can compose them because the vault serves both,
    and the alias system lets us access both cipher versions while
    also using formatter — all in one process, no conflicts.
    """
    from bubble.meta_finder import install

    # formatter is already in the vault. Install a finder that
    # resolves it, plus the cipher aliases for good measure.
    aliases = {
        "cipher_old": ("cipher", "1.0.0", "py3-none-any"),
        "cipher_new": ("cipher", "2.0.0", "py3-none-any"),
    }
    finder = install(aliases=aliases, verbose=False)
    loaded_before = set(sys.modules)

    import formatter  # noqa: E402

    # Build a regression report: formatter renders the diff table.
    rows = []
    for v1, v2 in zip(v1_results, v2_results):
        rows.append((
            v1["input"][:20],
            v1["v1"][:25],
            v2["v2"][:25],
            "MATCH" if v1["v1"] == v2["v2"] else "DIFF",
        ))

    report = formatter.banner("REGRESSION HUNT: cipher v1 vs v2")
    report += "\n\n"
    report += formatter.table(rows, headers=("input", "v1 output", "v2 output", "status"))

    # Clean up
    for mod in list(sys.modules):
        if mod not in loaded_before:
            del sys.modules[mod]
    if finder in sys.meta_path:
        sys.meta_path.remove(finder)

    return report


# ─── rendering ───────────────────────────────────────────────────────

def _render_multiversion(results):
    """Render the multi-version comparison as a structured report."""
    lines = [
        "╔══════════════════════════════════════════════════════════════╗",
        "║  IMPOSSIBLE THING #1: Two versions, one process            ║",
        "║  Standard Python: import cipher gives ONE version.           ║",
        "║  Bubble: import cipher_old AND cipher_new, side-by-side.    ║",
        "╚══════════════════════════════════════════════════════════════╝",
        "",
    ]

    for r in results:
        v1_info = r["v1_info"]
        v2_info = r["v2_info"]
        lines.append(f"  input:    {r['input']!r}")
        lines.append(f"  v1:       {r['v1']!r}  ({v1_info['method']}, {v1_info.get('flaw', v1_info.get('fix', ''))})")
        lines.append(f"  v2:       {r['v2']!r}  ({v2_info['method']}, {v2_info.get('fix', v2_info.get('flaw', ''))})")
        if r["match"]:
            lines.append(f"  status:   identical output")
        else:
            lines.append(f"  status:   *** DIFFERENT — behavioral regression detected ***")
        lines.append("")

    # Summary
    diffs = sum(1 for r in results if not r["match"])
    lines.append(f"  {len(results)} inputs tested. {diffs} behavioral differences found.")
    lines.append(f"  v1 drops non-alpha chars and lowercases. v2 preserves them.")
    lines.append(f"  This diff would be invisible in pip — you only ever get one version.")
    return "\n".join(lines)


def _render_contention(contentions):
    """Render the contention audit."""
    lines = [
        "╔══════════════════════════════════════════════════════════════╗",
        "║  IMPOSSIBLE THING #2: Contention audit                      ║",
        "║  pip: last install wins. Silent. You never know.            ║",
        "║  Bubble: the contention is recorded and auditable.           ║",
        "╚══════════════════════════════════════════════════════════════╝",
        "",
    ]

    if not contentions:
        lines.append("  (no contentions found — each import name is claimed by exactly one package)")
    else:
        for c in contentions:
            lines.append(f"  import name: {c['import_name']!r}")
            lines.append(f"    incoming:  {c['incoming'][0]} v{c['incoming'][1]} [{c['incoming'][2]}]")
            for ex in c["existing"]:
                lines.append(f"    existing:  {ex[0]} v{ex[1]} [{ex[2]}]")
            lines.append(f"    → first-claim semantics: the existing package keeps the name")
            lines.append(f"    → the incoming package is recorded but doesn't override")
            lines.append("")

    lines.append("  In pip, 'pip install cryptolib' after 'pip install cipher'")
    lines.append("  silently replaces the 'cipher' import. Bubble records the collision.")
    return "\n".join(lines)


def _render_archaeology(data):
    """Render the vault archaeology report."""
    lines = [
        "╔══════════════════════════════════════════════════════════════╗",
        "║  IMPOSSIBLE THING #3: Content-addressed archaeology          ║",
        "║  pip knows dependencies. It does not know content hashes or  ║",
        "║  import-name collisions. The vault does.                      ║",
        "╚══════════════════════════════════════════════════════════════╝",
        "",
    ]

    lines.append("  Import names served by the vault:")
    for entry in data["import_names"]:
        lines.append(f"    {entry['name']:<20} ← claimed by {entry['claimed_by']} package(s)")
    lines.append("")

    lines.append("  Packages in the vault:")
    for pkg in data["packages"]:
        lines.append(f"    {pkg['name']:<16} {pkg['version']:<10} {pkg['tag']:<18} source={pkg['source']}")
    lines.append("")

    lines.append("  Content hashes (import → cryptographic edge):")
    for h in data["content_hashes"]:
        lines.append(f"    {h['import']:<16} → {h['package']} {h['version']}  sha256:{h['sha256'][:12]}…")
    lines.append("")

    lines.append("  Each import_name row in top_level carries import_sha256,")
    lines.append("  computed over the subtree it claims. The name-to-artifact")
    lines.append("  binding is cryptographic, not nominal. pip cannot do this.")
    return "\n".join(lines)


# ─── main ────────────────────────────────────────────────────────────

def main():
    from bubble.vault import db

    db.init_db()

    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │  BUBBLE — IMPOSSIBLE THINGS DEMO                       │")
    print("  │  Three things you CANNOT do in standard Python.         │")
    print("  │  Self-contained. No network. No pip. No venv.          │")
    print("  └─────────────────────────────────────────────────────────┘")
    print()

    # ── Stage packages into the vault ──────────────────────────────

    # cipher v1.0.0 — the old version with known bugs
    _stage_package(
        name="cipher", version="1.0.0", wheel_tag="py3-none-any",
        import_name="cipher",
        init_source=CIPHER_V1_INIT,
        top_level_imports={"cipher"},
    )

    # cipher v2.0.0 — the fixed version
    _stage_package(
        name="cipher", version="2.0.0", wheel_tag="py3-none-any",
        import_name="cipher",
        init_source=CIPHER_V2_INIT,
        top_level_imports={"cipher"},
        submodules={"hashing": '''\
            """cipher.hashing — new in v2: integrity checking."""
            def sha256_short(text, length=16):
                import hashlib
                return hashlib.sha256(text.encode()).hexdigest()[:length]
        '''},
    )

    # cryptolib — a DIFFERENT package that claims the 'cipher' import name.
    # This simulates the real opencv-python / opencv-python-headless conflict.
    # pip: last install silently wins. Bubble: contention is recorded.
    _stage_package(
        name="cryptolib", version="1.0.0", wheel_tag="py3-none-any",
        import_name="cipher",
        init_source=CRYPTO_V1_INIT,
        top_level_imports={"cipher"},
    )

    # formatter — a text rendering library. Independent of cipher.
    # Demonstrates cross-library composition: formatter renders what
    # cipher encodes, and they coexist because the vault serves both.
    _stage_package(
        name="formatter", version="1.0.0", wheel_tag="py3-none-any",
        import_name="formatter",
        init_source=FORMATTTER_V1_INIT,
        top_level_imports={"formatter"},
    )

    print("  Staged 4 packages into the vault:")
    print("    cipher       1.0.0  (ROT13, drops non-alpha, lowercases)")
    print("    cipher       2.0.0  (ROT13+, preserves case and punctuation)")
    print("    cryptolib    1.0.0  (XOR cipher, also claims 'cipher' import)")
    print("    formatter    1.0.0  (ASCII rendering, composes with cipher)")
    print()

    # ── Demo 1: Multi-version A/B ─────────────────────────────────

    results = demo_multiversion()
    print(_render_multiversion(results))
    print()

    # ── Demo 2: Contention audit ──────────────────────────────────

    contentions = demo_contention()
    print(_render_contention(contentions))
    print()

    # ── Demo 3: Vault archaeology ─────────────────────────────────

    arch_data = demo_vault_archaeology()
    print(_render_archaeology(arch_data))
    print()

    # ── Demo 4: Cross-library synthesis ──────────────────────────

    v1_results = [r for r in results]
    v2_results = [r for r in results]  # same inputs, different outputs already captured
    report = demo_cross_library_synthesis(v1_results, v2_results)

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  IMPOSSIBLE THING #4: Cross-library synthesis               ║")
    print("║  formatter (a rendering lib) composes with cipher (a crypto  ║")
    print("║  lib). Both cipher versions are accessible. The vault serves ║")
    print("║  all three. No dependency hell. No venv. No pip install.     ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print(report)
    print()

    # ── Summary ────────────────────────────────────────────────────

    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │  What you just saw:                                      │")
    print("  │                                                           │")
    print("  │  1. Two versions of the same library loaded in-process.   │")
    print("  │     Standard Python: impossible. pip resolves to one.    │")
    print("  │                                                           │")
    print("  │  2. Contention audit: two packages claim 'cipher'.        │")
    print("  │     pip: silent overwrite. Bubble: recorded, auditable.  │")
    print("  │                                                           │")
    print("  │  3. Cryptographic binding from import name to artifact.  │")
    print("  │     pip has no concept of this. The vault does.           │")
    print("  │                                                           │")
    print("  │  4. Three libraries composed in one process — one of     │")
    print("  │     them in two versions — with no dependency resolution. │")
    print("  │     The import IS the dependency declaration.             │")
    print("  └─────────────────────────────────────────────────────────┘")
    print()

    # ── Clean up ───────────────────────────────────────────────────

    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()