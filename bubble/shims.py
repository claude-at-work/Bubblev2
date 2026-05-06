"""shims — environment compensations for Termux/proot/Alpine/Kali.

The legacy code (legacy/bubble.py:68-231) maintained a PATH_SHIMS table and
built a per-bubble sysroot of symlinks. The new code runs in-process — there
is no per-bubble sysroot. So this module discovers the same things and
exposes them as environment-variable overrides instead.

The #1 breakage on these hosts is SSL: packages look for
/etc/ssl/certs/ca-certificates.crt but on Termux it lives at
$PREFIX/etc/tls/cert.pem, on Alpine at /etc/ssl/cert.pem, on RHEL at
/etc/pki/tls/certs/ca-bundle.crt. `apply()` finds whichever is present and
sets SSL_CERT_FILE / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE / NODE_EXTRA_CA_CERTS
to point there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SSL_CERT_CANDIDATES = [
    "{PREFIX}/etc/tls/cert.pem",          # Termux
    "{PREFIX}/etc/ssl/cert.pem",          # Termux variant
    "/etc/ssl/certs/ca-certificates.crt", # Debian/Kali under proot
    "/etc/ssl/cert.pem",                  # Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",   # RHEL/Fedora
    "{CERTIFI}",                          # Python certifi as last resort
]

SSL_CERT_DIR_CANDIDATES = [
    "{PREFIX}/etc/tls",
    "{PREFIX}/etc/ssl/certs",
    "/etc/ssl/certs",
]

RESOLV_CONF_CANDIDATES = [
    "/etc/resolv.conf",
    "{PREFIX}/etc/resolv.conf",
]

# The env vars that, when set to a cert bundle path, cover most Python
# and JS HTTPS clients.
SSL_ENV_VARS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)


@dataclass
class ShimReport:
    """What was found, what wasn't, what got applied to os.environ."""
    cert_file: Optional[Path] = None
    cert_dir: Optional[Path] = None
    resolv_conf: Optional[Path] = None
    applied: list[tuple[str, str]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def ssl_ready(self) -> bool:
        return self.cert_file is not None


def _resolve(template: str) -> Optional[str]:
    """Substitute {PREFIX}/{TMPDIR}/{CERTIFI} in a path template."""
    prefix = os.environ.get("PREFIX", "/usr")
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    s = template.replace("{PREFIX}", prefix).replace("{TMPDIR}", tmpdir)
    if "{CERTIFI}" in s:
        try:
            import certifi
            return certifi.where()
        except ImportError:
            return None
    return s


def _first_existing(candidates: list[str]) -> Optional[Path]:
    for tmpl in candidates:
        resolved = _resolve(tmpl)
        if resolved and os.path.exists(resolved):
            return Path(resolved)
    return None


def discover() -> ShimReport:
    """Probe the host for cert bundles and resolver config.

    Pure read — no environment mutation. doctor and preflight call this
    to report shim status without changing process state.
    """
    rpt = ShimReport()
    rpt.cert_file = _first_existing(SSL_CERT_CANDIDATES)
    rpt.cert_dir = _first_existing(SSL_CERT_DIR_CANDIDATES)
    rpt.resolv_conf = _first_existing(RESOLV_CONF_CANDIDATES)
    if not rpt.cert_file:
        rpt.gaps.append("SSL cert bundle")
    if not rpt.resolv_conf:
        rpt.gaps.append("/etc/resolv.conf")
    return rpt


def apply(report: Optional[ShimReport] = None) -> ShimReport:
    """Discover (if not provided) and mutate os.environ.

    Idempotent: a user-set env var pointing to an existing path is left
    alone. A user-set var pointing to a non-existent path gets overwritten
    with whatever the discovery found (otherwise `bubble run` inherits a
    broken hint and HTTPS still fails).
    """
    rpt = report or discover()
    if not rpt.cert_file:
        return rpt
    cert_str = str(rpt.cert_file)
    for var in SSL_ENV_VARS:
        existing = os.environ.get(var)
        if existing and os.path.exists(existing):
            continue
        os.environ[var] = cert_str
        rpt.applied.append((var, cert_str))
    return rpt


def env_overrides() -> dict[str, str]:
    """Return SSL-related env overrides without mutating the current process.

    For subprocess invocations (shell exec, bridge to legacy) where you want
    to pass `env=` rather than inherit the parent's mutations.
    """
    rpt = discover()
    if not rpt.cert_file:
        return {}
    cert_str = str(rpt.cert_file)
    return {var: cert_str for var in SSL_ENV_VARS}
