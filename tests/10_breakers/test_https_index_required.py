"""Claim: BUBBLE_PYPI_INDEX must be https — http and other schemes are
refused at fetch time.

Conventional intuition: per-file sha256 checks make transport security
optional — the hashes are bound. They aren't: a poisoned http response
can rewrite the JSON to swap in attacker-published hashes that match
attacker-published files on the (separately allowlisted) CDN. TLS is the
authentication on the channel that says "this hash came from the index
we trust"; without TLS we have nothing.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    cases = [
        ("http://pypi.org/simple", "http"),
        ("ftp://pypi.org/simple", "ftp"),
        ("file:///tmp/index", "file"),
    ]
    for url, kind in cases:
        os.environ["BUBBLE_PYPI_INDEX"] = url
        for mod in list(sys.modules):
            if mod.startswith("bubble"):
                del sys.modules[mod]
        from bubble.vault import fetcher
        try:
            fetcher.fetch_simple_index("any-package")
        except ValueError as exc:
            msg = str(exc)
            assert "https" in msg, f"{kind!r}: refusal didn't mention https: {msg}"
            r.evidence.append(f"{kind:6s} refused: {msg[:80]}")
        else:
            raise AssertionError(f"expected refusal for {url!r}, got success")
    os.environ.pop("BUBBLE_PYPI_INDEX", None)
    r.passed = True


if __name__ == "__main__":
    run_test(
        "BUBBLE_PYPI_INDEX must be https; http / file / ftp / etc. are "
        "refused at fetch time — per-file sha256 only authenticates a "
        "channel we already trust, and TLS is the only thing making the "
        "index responses themselves trustworthy",
        body,
    )
