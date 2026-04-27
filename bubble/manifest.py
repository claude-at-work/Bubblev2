"""Deployment manifest — the contract for shipment.

A deployment manifest names exactly what a shell ought to contain, in a
form that can be fed to `bubble shell create --from`, bundled along with
the vault subset it pins, and unbundled on a target machine to reproduce
the environment byte-for-byte.

It is a new file format introduced for this purpose. The `[packages]`
section happens to share a row shape with the older scope manifest the
meta-finder reads on `bubble run --scope`, so a deployment manifest can
also be passed to `--scope`; that overlap is a convenience, not a
foundation. The shape that matters here is the deployment shape: a
`name`, a pinned closure, and (reserved for the substrate-routing
thread) a per-alias substrate declaration.

Format:

    # bubble deployment manifest
    name = "my-app"

    [packages]
    "requests" = { version = "2.33.1", wheel_tag = "py3-none-any" }
    "urllib3"  = { version = "2.6.3",  wheel_tag = "py3-none-any" }

    [aliases]
    numpy_old = { name = "numpy", version = "1.26.4", wheel_tag = "...", substrate = "dlmopen_isolated" }
    numpy_new = { name = "numpy", version = "2.4.4",  wheel_tag = "..." }

The substrate field is reserved here so the format doesn't break when
the substrate-routing thread wires it through. Today the field is read
and stored but doesn't yet route — meta_finder still defaults all
aliases to in_process. That join is staged below this one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AliasPin:
    name: str            # the dist name (e.g. "numpy")
    version: str
    wheel_tag: str
    substrate: Optional[str] = None  # reserved for C5; None = default routing


@dataclass
class Manifest:
    name: Optional[str] = None
    packages: dict[str, tuple[str, str]] = field(default_factory=dict)
    # alias_name → AliasPin
    aliases: dict[str, AliasPin] = field(default_factory=dict)

    def to_scope(self) -> dict[str, tuple[str, str]]:
        """The subset the meta-finder's scope path reads — same shape as
        the existing scope manifest format."""
        return dict(self.packages)

    def to_alias_table(self) -> dict[str, tuple[str, str, str]]:
        """The subset the meta-finder's alias path reads — drops the
        substrate field, since meta_finder.install doesn't yet route on
        it. When C5 wires substrate routing, this method gains a
        substrate-aware sibling."""
        return {
            alias: (pin.name, pin.version, pin.wheel_tag)
            for alias, pin in self.aliases.items()
        }


# ───────────────────────── parsing ─────────────────────────────


_INLINE_FIELD_RE = re.compile(
    r'(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"'
)


def _parse_inline_table(s: str) -> dict[str, str]:
    """Parse `{ a = "x", b = "y", ... }` → {a: x, b: y}. Only handles
    string-valued fields, which is all the manifest format emits."""
    s = s.strip()
    if not (s.startswith("{") and s.endswith("}")):
        raise ValueError(f"not an inline table: {s!r}")
    out: dict[str, str] = {}
    for m in _INLINE_FIELD_RE.finditer(s[1:-1]):
        key, val = m.group(1), m.group(2)
        out[key] = val.replace('\\"', '"').replace("\\\\", "\\")
    return out


_TOP_KV_RE = re.compile(r'^([A-Za-z_][\w]*)\s*=\s*"([^"]*)"\s*$')
_PKG_LINE_RE = re.compile(r'^"([^"]+)"\s*=\s*(\{.+\})\s*$')
_ALIAS_LINE_RE = re.compile(r'^([A-Za-z_][\w]*)\s*=\s*(\{.+\})\s*$')


def load(path: Path) -> Manifest:
    """Read a deployment manifest. Tolerates absent file (returns empty
    Manifest) — callers can build a manifest programmatically and write."""
    m = Manifest()
    if not path.exists():
        return m

    section: Optional[str] = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section is None:
            tm = _TOP_KV_RE.match(line)
            if tm and tm.group(1) == "name":
                m.name = tm.group(2)
            continue
        if section == "packages":
            pm = _PKG_LINE_RE.match(line)
            if not pm:
                continue
            pkg = pm.group(1)
            fields = _parse_inline_table(pm.group(2))
            ver = fields.get("version")
            tag = fields.get("wheel_tag")
            if ver and tag:
                m.packages[pkg] = (ver, tag)
        elif section == "aliases":
            am = _ALIAS_LINE_RE.match(line)
            if not am:
                continue
            alias = am.group(1)
            fields = _parse_inline_table(am.group(2))
            real = fields.get("name")
            ver = fields.get("version")
            tag = fields.get("wheel_tag")
            if real and ver and tag:
                m.aliases[alias] = AliasPin(
                    name=real, version=ver, wheel_tag=tag,
                    substrate=fields.get("substrate"),
                )
    return m


# ───────────────────────── writing ──────────────────────────────


def dump(manifest: Manifest, path: Path) -> None:
    """Emit a deployment manifest. Stable order — packages alphabetical,
    aliases alphabetical — so two manifests of the same closure produce
    byte-identical files (a property the bundle thread will lean on)."""
    lines: list[str] = ["# bubble deployment manifest"]
    if manifest.name:
        lines.append(f'name = "{_escape(manifest.name)}"')
    lines.append("")

    if manifest.packages:
        lines.append("[packages]")
        for pkg in sorted(manifest.packages):
            ver, tag = manifest.packages[pkg]
            lines.append(
                f'"{_escape(pkg)}" = {{ '
                f'version = "{_escape(ver)}", '
                f'wheel_tag = "{_escape(tag)}" }}'
            )
        lines.append("")

    if manifest.aliases:
        lines.append("[aliases]")
        for alias in sorted(manifest.aliases):
            pin = manifest.aliases[alias]
            parts = [
                f'name = "{_escape(pin.name)}"',
                f'version = "{_escape(pin.version)}"',
                f'wheel_tag = "{_escape(pin.wheel_tag)}"',
            ]
            if pin.substrate:
                parts.append(f'substrate = "{_escape(pin.substrate)}"')
            lines.append(f"{alias} = {{ " + ", ".join(parts) + " }")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ─────────────────── derivation from an existing shell ───────────────────


def from_shell(shell_dir: Path) -> Manifest:
    """Reverse: read a shell's state manifest and produce a deployment
    manifest. Useful for `bubble shell freeze <name> -o app.manifest.toml`
    (a future thread). Aliases are not in the shell state manifest, so
    only `[packages]` is populated by this path."""
    from .run.shell import _read_manifest
    state = _read_manifest(shell_dir)
    m = Manifest(name=shell_dir.name)
    for pkg, info in state.items():
        m.packages[pkg] = (info["version"], info["wheel_tag"])
    return m
