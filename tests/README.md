# tests/

A test suite that doubles as the pitch.

Each test is an architectural claim about Bubble that conventional Python
intuition would call impossible. The tests use the real `bubble` package on
this machine — synthetic packages staged into a temp vault, not network
fixtures — so the suite is hermetic, fast, and offline-safe.

## Run

```
python3 tests/run.py                 # run everything
python3 tests/run.py 10_breakers     # filter by tier
python3 tests/run.py --no-md         # skip the gallery
```

After a run, `tests/RESULTS.md` is the gallery: each test's claim, evidence,
and timing, in a form that's pleasant to read.

## What's here

| tier | what it proves |
|------|----------------|
| `00_sanity/` | the vault DB initializes on the v2 schema |
| `10_breakers/` | the perception-breakers — coexistence, demand paging, name-bridging |
| `30_loop/` | the probe → consult → record → consult loop |

A few tests in `30_loop/` deliberately mark gaps the README admits to:
the recording channel is plumbed end-to-end, but the second half — failures
*altering next-run strategy* — is not yet load-bearing. When that ships, the
xfail flips, and the suite tells you so.

## How tests are isolated

Each test is invoked as a subprocess by the runner. Before importing any
`bubble.*` module, the test sets `BUBBLE_HOME` to a fresh tempdir, so:

- the vault DB is empty
- `~/.bubble` on the user's machine is never touched
- `sys.meta_path` and `sys.modules` can't leak between tests

The runner harvests a JSON line tagged `__BUBBLE_TEST_RESULT__` from each
test's stdout. Tests that crash without producing one are flagged with their
captured stdout/stderr.

## Adding a test

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, stage_fake_package, vault_finder, Result


def body(r: Result):
    # do real work; populate r.evidence; raise on failure
    r.evidence.append("what you observed")
    r.passed = True


if __name__ == "__main__":
    run_test("the architectural claim, in plain English", body)
```

Stage synthetic packages into the vault via `stage_fake_package(...)`.
Install the meta-finder via `vault_finder(aliases=...)`. Don't reach for
network or PyPI — the tests must be hermetic.
