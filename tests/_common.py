"""Shared test fixtures.

A test gets:
  - A fresh BUBBLE_HOME tempdir (set in env BEFORE importing bubble.*).
  - A way to stage synthetic packages into the vault — so tests don't need
    PyPI, don't need network, don't touch the user's real ~/.bubble.

A synthetic package is a real one for the vault's purposes: __init__.py with
the source of our choosing, plus a dist-info dir with METADATA, WHEEL, and
top_level.txt. Once committed via store.commit, the meta_finder treats it the
same as any other package.

Each test file's main() returns a Result; the runner aggregates.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Result:
    claim: str
    passed: bool = False
    skipped: Optional[str] = None
    error: Optional[str] = None
    evidence: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def _isolate_bubble_home() -> Path:
    """Create a tempdir, point BUBBLE_HOME at it, return the path.

    Must be called BEFORE the first `import bubble.*` in the process, since
    bubble.config reads BUBBLE_HOME at module load.
    """
    home = Path(tempfile.mkdtemp(prefix="bubble-test-"))
    os.environ["BUBBLE_HOME"] = str(home)
    return home


def stage_fake_package(
    *,
    name: str,
    version: str,
    import_name: Optional[str] = None,
    init_source: str = "",
    submodules: Optional[dict[str, str]] = None,
    wheel_tag: str = "py3-none-any",
    flat: bool = False,
    requires_dist: Optional[list[str]] = None,
    overwrite: bool = False,
) -> tuple[str, str, str, Path]:
    """Build a synthetic package on disk and commit it to the vault.

    `flat=True` stages a single-file module (`<import_name>.py` at the root)
    rather than a package directory; submodules are not allowed in this mode.

    `requires_dist` is a list of PEP 508 strings written as `Requires-Dist:`
    headers in METADATA — used to exercise dependency parsing.

    Returns (name, version, wheel_tag, vault_path).
    """
    from bubble.vault import store
    from bubble.vault import db

    db.init_db()

    import_name = import_name or name
    staged = store.stage_dir()

    if flat:
        if submodules:
            raise ValueError("flat modules cannot have submodules")
        (staged / f"{import_name}.py").write_text(textwrap.dedent(init_source).lstrip())
    else:
        pkg_dir = staged / import_name
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text(textwrap.dedent(init_source).lstrip())
        for sub_name, sub_src in (submodules or {}).items():
            (pkg_dir / f"{sub_name}.py").write_text(textwrap.dedent(sub_src).lstrip())

    dist_info = staged / f"{name}-{version}.dist-info"
    dist_info.mkdir()
    metadata_lines = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
    ]
    for req in requires_dist or []:
        metadata_lines.append(f"Requires-Dist: {req}")
    (dist_info / "METADATA").write_text("\n".join(metadata_lines) + "\n\n")
    (dist_info / "WHEEL").write_text(
        f"Wheel-Version: 1.0\nGenerator: bubble-test\nRoot-Is-Purelib: true\n"
        f"Tag: {wheel_tag}\n"
    )
    (dist_info / "top_level.txt").write_text(import_name + "\n")

    py_tag, abi_tag, plat_tag = (wheel_tag.split("-") + ["py3", "none", "any"])[:3]
    vault_path = store.commit(
        name=name,
        version=version,
        wheel_tag=wheel_tag,
        python_tag=py_tag,
        abi_tag=abi_tag,
        platform_tag=plat_tag,
        staged=staged,
        source="test",
        overwrite=overwrite,
    )
    return name, version, wheel_tag, vault_path


@contextmanager
def vault_finder(*, scope=None, aliases=None, autofetch=False):
    """Install a VaultFinder for the duration of the block; remove on exit."""
    from bubble.meta_finder import install
    finder = install(scope=scope, aliases=aliases, autofetch=autofetch)
    try:
        yield finder
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)


def run_test(claim: str, fn: Callable[[Result], None]) -> Result:
    """Run a test function; emit JSON to stdout for the runner to slurp.

    The function takes a Result and mutates it (adds evidence, sets passed,
    sets skipped). Exceptions become error+passed=False.
    """
    result = Result(claim=claim)
    home = _isolate_bubble_home()
    t0 = time.monotonic()
    try:
        fn(result)
        if result.skipped is None and result.error is None:
            # If the test didn't explicitly fail or skip, treat it as passed.
            # Tests that need to assert should raise on failure or set passed.
            if not result.evidence and not result.passed:
                result.passed = True
            elif not result.passed and result.error is None and result.skipped is None:
                result.passed = True
    except AssertionError as exc:
        result.passed = False
        result.error = f"AssertionError: {exc}"
    except Exception as exc:
        result.passed = False
        result.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    finally:
        result.elapsed_ms = int((time.monotonic() - t0) * 1000)
        shutil.rmtree(home, ignore_errors=True)
    sys.stdout.write("\n__BUBBLE_TEST_RESULT__" + result.to_json() + "\n")
    sys.stdout.flush()
    return result
