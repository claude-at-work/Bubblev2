"""Claim: the index's canonical name must match the requested name (PEP
503 normalized) — a swap is refused before vaulting.

Conventional intuition: PyPI's simple-API echoes back the canonical
name; it's metadata, take it at face value. Bubble's stance: if the
index returned `name: requests` for our request `flask`, *something is
wrong* — poisoned response, server bug, attacker-in-the-middle. We
shouldn't quietly vault the bytes under a name we never asked for.

Hermetic: a mock simple-API returns `name: substituted` for a request
for `original`. The fetcher must refuse before any download.
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


def _serve_fake_index(payload: dict):
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
    return f"http://127.0.0.1:{port}/simple", httpd


def body(r: Result):
    payload = {
        "name": "substituted",
        "files": [{
            "filename": "substituted-1.0.0-py3-none-any.whl",
            "url": "https://files.pythonhosted.org/packages/aa/bb/substituted-1.0.0-py3-none-any.whl",
            "hashes": {"sha256": "0" * 64},
            "yanked": False,
        }],
    }
    base, httpd = _serve_fake_index(payload)
    try:
        os.environ["BUBBLE_PYPI_INDEX"] = base
        for mod in list(sys.modules):
            if mod.startswith("bubble"):
                del sys.modules[mod]
        from bubble.vault import fetcher
        fetcher._validate_index_url = lambda u: None  # localhost test exemption

        try:
            fetcher.fetch_into_vault("original")
        except ValueError as exc:
            msg = str(exc)
            assert "substituted" in msg or "original" in msg, \
                f"refusal didn't name the mismatch: {msg}"
            assert "refusing" in msg or "didn't ask for" in msg, \
                f"refusal didn't surface the trust break: {msg}"
            r.evidence.append("name swap refused before any download")
            r.evidence.append(f"  message: {msg[:120]}")
        else:
            raise AssertionError("expected ValueError on name swap; none raised")
    finally:
        os.environ.pop("BUBBLE_PYPI_INDEX", None)
        httpd.shutdown()
        httpd.server_close()
    r.passed = True


if __name__ == "__main__":
    run_test(
        "the canonical name returned by the index is cross-validated "
        "against the requested name (PEP 503 normalized) — a swap "
        "refuses before download, so the vault never holds bytes under "
        "a name the operator didn't request",
        body,
    )
