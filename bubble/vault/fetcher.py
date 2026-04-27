"""Minimal PyPI client. Stdlib-only (urllib + json + zipfile).

Uses the JSON simple-API:
    GET /simple/<name>/  with Accept: application/vnd.pypi.simple.v1+json
    https://packaging.python.org/en/latest/specifications/simple-repository-api/

Wheels are downloaded and unpacked into the vault. sdists are refused by
default — they require running the dist's `setup.py` (or PEP 517 backend),
which is arbitrary code execution at vault-add time, a sovereignty break
the vault is built to prevent. Override with `--allow-sdist` (CLI) or
BUBBLE_ALLOW_SDIST=1 (env) and accept the trust boundary explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterator, Optional

from .. import config
from . import db, store, metadata as meta


PYPI_INDEX = os.environ.get("BUBBLE_PYPI_INDEX", "https://pypi.org/simple")
USER_AGENT = f"bubble/0.3.0 (+stdlib; python {sys.version_info.major}.{sys.version_info.minor})"


def _validate_index_url(url: str) -> None:
    """Refuse non-https index URLs at fetch time. A poisoned index can be
    held in tension with the per-file sha256 — but only if the channel itself
    is authenticated. http:// drops that authentication."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme != "https":
        raise ValueError(
            f"refusing non-https index URL: {url!r} "
            f"(BUBBLE_PYPI_INDEX must use https; sovereignty default)"
        )
    if not parts.hostname:
        raise ValueError(f"index URL has no host: {url!r}")


# files.pythonhosted.org is the canonical artifact CDN PyPI's simple-API
# redirects wheel downloads to. Allow that plus the index host. A poisoned
# simple-API response that tries to redirect downloads to file:// or an
# attacker-controlled host fails closed before any bytes are fetched.
_ALLOWED_DOWNLOAD_HOSTS = frozenset({
    urllib.parse.urlparse(PYPI_INDEX).hostname or "pypi.org",
    "files.pythonhosted.org",
})


def _download_url_ok(url: str) -> bool:
    """Validate that an index-supplied download URL is https and on a known host."""
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in _ALLOWED_DOWNLOAD_HOSTS


# ───────────────────────────── wheel filenames ──────────────────────────


_WHL_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>[^-]+)"
    r"(?:-(?P<build>\d[^-]*))?"
    r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)


def parse_wheel_filename(filename: str) -> Optional[dict]:
    m = _WHL_RE.match(filename)
    if not m:
        return None
    d = m.groupdict()
    d["tag"] = f"{d['py']}-{d['abi']}-{d['plat']}"
    return d


# ─────────────────── version sort & wheel-tag scoring ────────────────────


def _version_key(v: str) -> tuple:
    parts = []
    for chunk in re.split(r"[.\-+]", v):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


def _is_prerelease(v: str) -> bool:
    # Quick check; PEP 440 is more nuanced but this catches the common cases
    return bool(re.search(r"[abc]|rc|dev|alpha|beta", v, re.I))


def _wheel_tag_score(py: str, abi: str, plat: str) -> int:
    """Higher = better-matching for runner. 0 = incompatible."""
    runner_py = config.runner_python_tag()
    runner_plat = config.runner_platform_tag()
    # Extract the runner architecture (e.g. linux_aarch64 → aarch64)
    runner_arch = runner_plat.split("_", 1)[1] if "_" in runner_plat else runner_plat

    # Hard incompatibilities first
    if plat != "any":
        # Architecture must match. manylinux/musllinux/etc. embed the arch.
        if runner_arch not in plat:
            return 0

    if abi != "none" and abi != "abi3" and abi != runner_py:
        return 0

    score = 0
    if py == runner_py:
        score += 100
    elif py == "py3" or py.startswith("py3"):
        score += 30
    elif py.startswith(runner_py[:2]):  # cp* matches cp*
        score += 20
    else:
        return 0  # python-version mismatch is a hard fail

    if abi == "abi3":
        score += 50
    elif abi == "none":
        score += 10
    elif abi == runner_py:
        score += 60

    if plat == "any":
        score += 5
    elif runner_plat in plat or plat in runner_plat:
        score += 40
    elif plat.startswith("manylinux"):
        score += 30  # most manylinux wheels work on most glibc systems

    return score


# ───────────────────────────── HTTP fetching ─────────────────────────────


def _http_get(url: str, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_simple_index(name: str) -> dict:
    """GET /simple/<name>/ with JSON accept. Returns parsed JSON."""
    _validate_index_url(PYPI_INDEX)
    norm = meta.normalize_name(name)
    url = f"{PYPI_INDEX.rstrip('/')}/{norm}/"
    try:
        data = _http_get(url, accept="application/vnd.pypi.simple.v1+json")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError(f"package not found on index: {name}")
        raise
    return json.loads(data.decode("utf-8"))


# ───────────────────────────── pick & download ───────────────────────────


def pick_release(
    files: list[dict],
    *,
    pinned_version: Optional[str] = None,
    allow_prerelease: bool = False,
) -> Optional[dict]:
    """Choose the best file (wheel, with sdist fallback) from the simple-API listing.

    Each file is {filename, url, hashes, requires-python, yanked, ...}.
    """
    candidates = []
    for f in files:
        if f.get("yanked"):
            continue
        fn = f.get("filename", "")
        if not (fn.endswith(".whl") or fn.endswith(".tar.gz") or fn.endswith(".zip")):
            continue
        if fn.endswith(".whl"):
            parsed = parse_wheel_filename(fn)
            if not parsed:
                continue
            # Wheels may compress multiple tags: py2.py3-none-any
            best_score = 0
            best_sub = None
            for py_part in parsed["py"].split("."):
                for abi_part in parsed["abi"].split("."):
                    for plat_part in parsed["plat"].split("."):
                        s = _wheel_tag_score(py_part, abi_part, plat_part)
                        if s > best_score:
                            best_score = s
                            best_sub = (py_part, abi_part, plat_part)
            if best_score == 0:
                continue
            score = best_score
            version = parsed["version"]
            kind = "wheel"
            # Re-record the matched sub-tag for storage
            parsed = {**parsed, "py": best_sub[0], "abi": best_sub[1], "plat": best_sub[2],
                      "tag": "-".join(best_sub)}
        else:
            # sdist
            m = re.match(r"^(.+)-([^-]+)\.(?:tar\.gz|zip)$", fn)
            if not m:
                continue
            version = m.group(2)
            score = 1  # sdists rank below any wheel
            parsed = {"tag": "sdist", "py": "sdist", "abi": "sdist", "plat": "sdist"}
            kind = "sdist"
        if not allow_prerelease and _is_prerelease(version):
            continue
        if pinned_version and version != pinned_version:
            continue
        candidates.append((version, score, kind, parsed, f))

    if not candidates:
        return None
    # Highest version, then highest tag score, then prefer wheel over sdist
    candidates.sort(key=lambda c: (_version_key(c[0]), c[1], c[2] == "wheel"),
                    reverse=True)
    version, score, kind, parsed, f = candidates[0]
    return {"version": version, "score": score, "kind": kind, "parsed": parsed, "file": f}


def _download(url: str, dest: Path, expected_sha256: str) -> Path:
    """Download `url` to `dest`, verifying SHA-256. Hash is mandatory:
    PyPI's Simple API publishes a hash for every file, so a missing hash means
    the source is non-canonical or tampered — we'd rather fail loudly."""
    if not expected_sha256:
        raise ValueError(f"refusing to download without a published sha256: {url}")
    if not _download_url_ok(url):
        raise ValueError(f"refusing to download from non-allowlisted URL: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=120) as resp:
        with dest.open("wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
    actual = h.hexdigest()
    if actual != expected_sha256:
        dest.unlink(missing_ok=True)
        raise ValueError(f"sha256 mismatch for {url}: expected {expected_sha256}, got {actual}")
    return dest


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Reject zip-slip / absolute-path / symlink members before extraction.
    Uses the 3.12+ data filter when available; otherwise validates members manually."""
    dest_resolved = dest.resolve()
    for info in zf.infolist():
        name = info.filename
        if name.startswith("/") or ".." in Path(name).parts or "\x00" in name:
            raise ValueError(f"unsafe archive member: {name!r}")
        # Reject symlinks in zips (rare but possible via external_attr)
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"symlink in zip not permitted: {name!r}")
        target = (dest / name).resolve()
        if dest_resolved != target and dest_resolved not in target.parents:
            raise ValueError(f"archive member escapes target: {name!r}")
    zf.extractall(dest)


def fetch_into_vault(
    name: str,
    *,
    pinned_version: Optional[str] = None,
    allow_prerelease: bool = False,
    overwrite: bool = False,
    allow_sdist: bool = False,
) -> Optional[tuple[str, str, str]]:
    """High-level: pick best release, download, unpack into vault.

    Returns (name, version, wheel_tag) on success, None if already vaulted
    or no compatible release found.

    `allow_sdist=False` is the sovereignty default: sdists require running
    setup.py / a PEP 517 backend, which is arbitrary user-privileged code
    execution at vault-add. Caller must opt in explicitly to accept that
    trust boundary. Env var BUBBLE_ALLOW_SDIST=1 also flips the default.
    """
    db.init_db()
    if not allow_sdist and os.environ.get("BUBBLE_ALLOW_SDIST"):
        allow_sdist = True

    index = fetch_simple_index(name)
    canonical_name = index.get("name") or name

    # Cross-validate the canonical name against the requested name. The
    # JSON Simple-API echoes back its own canonical (PEP 503 normalized)
    # name; if it doesn't match what we asked for after normalization,
    # something is off (poisoned response, wrong package, redirect).
    if meta.normalize_name(canonical_name) != meta.normalize_name(name):
        raise ValueError(
            f"index returned name {canonical_name!r} for request {name!r}; "
            f"refusing to vault under a name we didn't ask for"
        )

    pick = pick_release(index.get("files", []),
                        pinned_version=pinned_version,
                        allow_prerelease=allow_prerelease)
    if not pick:
        return None

    version = pick["version"]
    wheel_tag = pick["parsed"]["tag"]
    py_tag = pick["parsed"]["py"]
    abi_tag = pick["parsed"]["abi"]
    plat_tag = pick["parsed"]["plat"]

    if pick["kind"] == "sdist" and not allow_sdist:
        raise RuntimeError(
            f"{canonical_name}=={version} is only available as an sdist; "
            f"vaulting an sdist runs its setup.py / build backend, which "
            f"the vault refuses by default. To opt in, pass --allow-sdist "
            f"(or BUBBLE_ALLOW_SDIST=1) and accept that trust boundary. "
            f"Alternative: install in a trusted venv and use `bubble vault "
            f"import-venv`."
        )

    conn = db.connect()
    if not overwrite and store.has(conn, canonical_name, version, wheel_tag):
        conn.close()
        return None
    conn.close()

    f = pick["file"]
    expected_sha = (f.get("hashes") or {}).get("sha256")
    download_dir = config.WHEELS_DIR / f"{canonical_name}_{version}"
    download_dir.mkdir(parents=True, exist_ok=True)
    artifact = download_dir / f["filename"]

    try:
        _download(f["url"], artifact, expected_sha256=expected_sha)
    except Exception:
        shutil.rmtree(download_dir, ignore_errors=True)
        raise

    staged = store.stage_dir()
    try:
        if pick["kind"] == "wheel":
            with zipfile.ZipFile(artifact) as zf:
                _safe_extract_zip(zf, staged)
        else:
            # sdist (allow_sdist already enforced above).
            sys.stderr.write(
                f"[bubble] WARNING: invoking pip to build sdist {canonical_name}"
                f"=={version} — setup.py / build backend will execute as "
                f"the current user.\n"
            )
            if not shutil.which("pip3") and not shutil.which("pip"):
                raise RuntimeError(
                    f"{canonical_name}=={version} sdist build requested but "
                    f"pip is not on PATH"
                )
            pip = shutil.which("pip3") or shutil.which("pip")
            subprocess.check_call([
                pip, "install", "--no-deps", "--target", str(staged),
                "--no-build-isolation", str(artifact),
            ])

        store.commit(
            name=canonical_name,
            version=version,
            wheel_tag=wheel_tag,
            python_tag=py_tag,
            abi_tag=abi_tag,
            platform_tag=plat_tag,
            staged=staged,
            source="pypi-sdist" if pick["kind"] == "sdist" else "pypi",
            sha256=expected_sha,
            metadata={
                "filename": f["filename"],
                "requires_python": f.get("requires-python"),
            },
            overwrite=overwrite,
        )
        return (canonical_name, version, wheel_tag)
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)
