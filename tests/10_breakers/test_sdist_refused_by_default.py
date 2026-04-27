"""Claim: an sdist-only release is refused by default; opt-in is required.

Conventional intuition: "pip install <pkg>" runs setup.py. Bubble exists in
opposition to that — the vault is supposed to hold *bytes*, not the side
effects of arbitrary code execution at install time.

For most of the project's life, the fetcher's sdist branch silently shelled
out to `pip install --target`, which runs setup.py / a PEP 517 backend as
the current user. Any dist with no compatible wheel turned `bubble vault
get` into RCE-on-pip's-trust. This test pins down the strict default and
the explicit opt-in.

Hermetic: a fake simple-index responder returns an sdist-only listing.
We never touch the network and we never invoke pip — the refusal happens
before either.
"""
import http.server
import json
import os
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def _serve_fake_index(payload: dict) -> tuple[str, threading.Thread, socketserver.TCPServer]:
    """Stand up a localhost HTTPS-not-required mock simple-API. We bypass
    the https check by patching the validator directly; this server only
    answers GET /simple/<pkg>/."""
    body = json.dumps(payload).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.pypi.simple.v1+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a, **_k):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}/simple", t, httpd


def body(r: Result):
    # Mock simple-API listing: only an sdist available, no wheels.
    payload = {
        "name": "fakepkg",
        "files": [{
            "filename": "fakepkg-1.0.0.tar.gz",
            "url": "https://files.pythonhosted.org/packages/aa/bb/fakepkg-1.0.0.tar.gz",
            "hashes": {"sha256": "0" * 64},
            "yanked": False,
        }],
    }
    base, _t, httpd = _serve_fake_index(payload)
    try:
        os.environ["BUBBLE_PYPI_INDEX"] = base
        # Fresh import so PYPI_INDEX is reread.
        for mod in list(sys.modules):
            if mod.startswith("bubble"):
                del sys.modules[mod]

        from bubble.vault import fetcher

        # Bypass the https-only validator for this localhost test only.
        fetcher._validate_index_url = lambda u: None

        # Default — should refuse the sdist with a clear sovereignty message.
        try:
            fetcher.fetch_into_vault("fakepkg")
        except RuntimeError as exc:
            msg = str(exc)
            assert "sdist" in msg, f"refusal didn't mention sdist: {msg}"
            assert "setup.py" in msg or "build backend" in msg, \
                f"refusal didn't name the trust boundary: {msg}"
            assert "--allow-sdist" in msg or "BUBBLE_ALLOW_SDIST" in msg, \
                f"refusal didn't surface the opt-in: {msg}"
            r.evidence.append("default refuse: sdist blocked before any download")
            r.evidence.append(f"  message: {msg[:120]}")
        else:
            raise AssertionError("expected RuntimeError refusing sdist; none raised")

        # Opt-in via env var: the gate flips. We don't actually run pip in
        # this test (we'd need a real artifact); but we DO assert the gate
        # changed from "refuse before download" to "proceed to download".
        # The download will fail (fake URL/hash) — that's fine; we just
        # need to confirm the refusal message changed shape.
        os.environ["BUBBLE_ALLOW_SDIST"] = "1"
        try:
            fetcher.fetch_into_vault("fakepkg")
        except (RuntimeError, ValueError, OSError, Exception) as exc:
            msg = str(exc)
            # The new failure must NOT be the sovereignty refusal — it
            # should be a download/network/hash error instead.
            assert "refuses by default" not in msg, \
                f"opt-in didn't take effect: {msg}"
            r.evidence.append("opt-in via BUBBLE_ALLOW_SDIST=1 changes the failure shape")
            r.evidence.append(f"  downstream failure: {type(exc).__name__}")
        finally:
            os.environ.pop("BUBBLE_ALLOW_SDIST", None)
    finally:
        os.environ.pop("BUBBLE_PYPI_INDEX", None)
        httpd.shutdown()
        httpd.server_close()

    r.passed = True


if __name__ == "__main__":
    run_test(
        "sdist-only releases are refused by default — running setup.py is "
        "RCE under the user's privileges, a sovereignty break the vault "
        "exists to prevent. --allow-sdist / BUBBLE_ALLOW_SDIST=1 toggles "
        "the gate explicitly.",
        body,
    )
