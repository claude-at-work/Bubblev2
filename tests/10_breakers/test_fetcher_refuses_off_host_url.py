"""Claim: the fetcher refuses to download from a URL that isn't on the
configured PyPI index host or files.pythonhosted.org. A poisoned simple-API
response that redirects downloads to file:// or an attacker-controlled host
fails closed before any bytes are fetched.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble.vault import fetcher

    bogus_sha = "0" * 64

    # http (not https) on the right host — refused.
    try:
        fetcher._download(
            "http://files.pythonhosted.org/packages/x/y/z.whl",
            Path("/tmp/_bubble_test_should_never_exist.whl"),
            expected_sha256=bogus_sha,
        )
        raise AssertionError("http URL was accepted")
    except ValueError as exc:
        assert "non-allowlisted" in str(exc), exc

    # https on a non-allowlisted host — refused.
    try:
        fetcher._download(
            "https://evil.example.com/wheel.whl",
            Path("/tmp/_bubble_test_should_never_exist.whl"),
            expected_sha256=bogus_sha,
        )
        raise AssertionError("off-host URL was accepted")
    except ValueError as exc:
        assert "non-allowlisted" in str(exc), exc

    # file:// — refused.
    try:
        fetcher._download(
            "file:///etc/passwd",
            Path("/tmp/_bubble_test_should_never_exist"),
            expected_sha256=bogus_sha,
        )
        raise AssertionError("file:// URL was accepted")
    except ValueError as exc:
        assert "non-allowlisted" in str(exc), exc

    # https on the allowlisted CDN host — passes the URL check (will fail
    # later on network/hash, which is fine; we're testing the gate).
    assert fetcher._download_url_ok(
        "https://files.pythonhosted.org/packages/x/y/z.whl"
    ), "canonical CDN URL must be allowlisted"

    r.evidence.append("http://files.pythonhosted.org/...           → rejected")
    r.evidence.append("https://evil.example.com/wheel.whl          → rejected")
    r.evidence.append("file:///etc/passwd                          → rejected")
    r.evidence.append("https://files.pythonhosted.org/...          → admitted")
    r.evidence.append("→ poisoned-mirror redirects fail before any bytes are fetched")
    r.passed = True


if __name__ == "__main__":
    run_test(
        "fetcher refuses non-allowlisted download URLs (off-host, http, file://) "
        "before any network work — a poisoned simple-API response can't redirect us",
        body,
    )
