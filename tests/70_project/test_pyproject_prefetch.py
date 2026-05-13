"""Fault loop: pyproject prefetch resolves vault miss before per-import fault fires."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result, stage_fake_package


def body(r: Result):
    """VaultFinder with autofetch=True and a pyproject.toml in cwd:
    the first _fault_to_pypi call should trigger _prefetch_pyproject_deps,
    which fetches declared-but-missing dists and then resolves the import
    from the vault without a per-import fetch.
    """
    from bubble.vault import db
    from bubble.meta_finder import VaultFinder

    db.init_db()
    # Stage 'gamma' in vault — simulates a successful fetch result
    stage_fake_package(name="gamma", version="1.0.0", import_name="gamma",
                       init_source='VALUE = 42')

    with tempfile.TemporaryDirectory() as tmp:
        pyproj = Path(tmp) / "pyproject.toml"
        pyproj.write_text(
            '[project]\n'
            'name = "demo"\n'
            'dependencies = ["gamma"]\n'
        )

        fetch_calls: list[str] = []

        def fake_fetch(dist, **kw):
            fetch_calls.append(dist)
            # gamma is already in vault (staged above) so returning None
            # simulates "already present / not re-fetched" — the vault hit
            # that follows should still succeed.
            return None

        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with patch("bubble.vault.fetcher.fetch_into_vault", side_effect=fake_fetch):
                finder = VaultFinder(autofetch=True, verbose=False)
                # _pyproject_prefetched starts False
                assert not finder._pyproject_prefetched

                # Trigger _fault_to_pypi for 'gamma' — gamma IS in the vault
                # (staged above) so _lookup succeeds after prefetch.
                result = finder._fault_to_pypi("gamma")

            r.evidence.append(f"result: {result}")
            r.evidence.append(f"prefetched flag: {finder._pyproject_prefetched}")
            r.evidence.append(f"pyproject_dists: {finder._pyproject_dists}")

            # Prefetch must have run
            assert finder._pyproject_prefetched, "_pyproject_prefetched not set"
            # The declared dep was loaded from the manifest
            assert "gamma" in finder._pyproject_dists, "gamma not in parsed dists"
            # gamma was already in vault so fetch_into_vault should NOT have
            # been called for it (deps_not_in_vault filters it out)
            assert "gamma" not in fetch_calls, (
                f"gamma should not be re-fetched when already in vault; "
                f"calls: {fetch_calls}"
            )
            # _lookup should have succeeded: result is a Path
            assert result is not None, "vault path expected after prefetch"
        finally:
            os.chdir(old_cwd)

    r.passed = True


if __name__ == "__main__":
    run_test("fault loop pyproject prefetch: declared dep resolved from vault without per-import fetch", body)
