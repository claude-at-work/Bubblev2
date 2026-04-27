#!/usr/bin/env python3
"""
bubble_cli — the human face of bubble.py
=========================================

Drop this next to bubble.py. Run it instead of bubble.py.

Usage:
    bubble <script.py> [args...]     # just run something
    bubble get <package> [...]       # pre-cache packages
    bubble status                    # what's in the vault
    bubble doctor                    # diagnose environment
    bubble clean                     # dissolve all active bubbles
    bubble preflight <script.py>     # offline readiness check

Flags:
    --yes / -y      never prompt, auto-confirm everything
    --quiet / -q    suppress all bubble output (agent mode)
    --keep          keep the bubble dir after run (debug)
    --version       show version and exit
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

__version__ = "0.2.0"

# ─────────────────────────────────────────────
# Terminal capabilities
# ─────────────────────────────────────────────

def _supports_color():
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return True

COLOR = _supports_color()

def _c(code, text):
    if not COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def dim(t):   return _c("2",     t)
def green(t): return _c("32",    t)
def amber(t): return _c("33",    t)
def red(t):   return _c("31",    t)
def cyan(t):  return _c("36",    t)
def bold(t):  return _c("1",     t)
def faint(t): return _c("2;37",  t)

def _hide_cursor():
    if COLOR: sys.stdout.write("\033[?25l"); sys.stdout.flush()

def _show_cursor():
    if COLOR: sys.stdout.write("\033[?25h"); sys.stdout.flush()

def _clear_line():
    if COLOR: sys.stdout.write("\r\033[2K")
    else:     sys.stdout.write("\r")
    sys.stdout.flush()


# ─────────────────────────────────────────────
# Pipeline stages
# ─────────────────────────────────────────────

# (name, color_fn, visual_weight)
STAGES = [
    ("scanning",   cyan,   1),
    ("resolving",  cyan,   1),
    ("fetching",   amber,  3),
    ("assembling", cyan,   1),
    ("running",    green,  4),
    ("dissolving", dim,    1),
]

STAGE_NAMES   = [s[0] for s in STAGES]
STAGE_COLORS  = {s[0]: s[1] for s in STAGES}
STAGE_WEIGHTS = [s[2] for s in STAGES]
TOTAL_WEIGHT  = sum(STAGE_WEIGHTS)

def _stage_frac(idx):
    return sum(STAGE_WEIGHTS[: idx + 1]) / TOTAL_WEIGHT

# Engine output line → stage trigger
STAGE_TRIGGERS = [
    (re.compile(r"scanning|scan",               re.I), "scanning"),
    (re.compile(r"resolv|vault check",          re.I), "resolving"),
    (re.compile(r"Downloading|Fetching|↓",      re.I), "fetching"),
    (re.compile(r"assembl|bubble:|Vaulted",     re.I), "assembling"),
    (re.compile(r"Run ──|retrying",             re.I), "running"),
    (re.compile(r"Dissolv|dissolved",           re.I), "dissolving"),
]

def _classify(line):
    for pat, stage in STAGE_TRIGGERS:
        if pat.search(line):
            return stage
    return None

def _is_error(line):
    return any(x in line for x in [
        "Traceback", "Error:", "error:", "✗", "FAILED",
        "ModuleNotFoundError", "ImportError", "Could not",
    ])

def _is_script_output(line):
    stripped = line.strip()
    if not stripped:
        return False
    internals = ["│", "├", "└", "◉", "◎", "↓", "✓", "✗",
                 "Bubble", "bubble", "Vault", "vault",
                 "Scan", "scan", "Dissolv", "assembl"]
    return not any(i in stripped for i in internals)


# ─────────────────────────────────────────────
# BubbleRunner
# ─────────────────────────────────────────────

BAR_WIDTH     = 28
PULSE_FRAMES  = ["·  ", "·· ", "···", " ··", "  ·", "   "]
BAR_FILLED    = "█"
BAR_TIP       = "▓"
BAR_EMPTY     = "░"
HANG_SECS     = 8.0


class BubbleRunner:
    """
    Runs bubble.py, parses its stdout line-by-line in a thread,
    and renders a live progress bar to the terminal.

    Honest progress:
      - Known stages advance the bar to real milestones.
      - 'running' holds the bar and pulses — no fake progress.
      - Silence > HANG_SECS shifts label to amber "waiting..."
        so the human knows something is slow, not broken.
      - On failure, captured output is printed cleanly below a rule.
    """

    def __init__(self, engine_path, args, script_name="script",
                 quiet=False):
        self.engine_path = engine_path
        self.args        = args
        self.script_name = script_name
        self.quiet       = quiet

        self._stage      = "scanning"
        self._stage_idx  = 0
        self._lock       = threading.Lock()
        self._last_t     = time.time()
        self._captured   = []
        self._script_out = []
        self._done       = False
        self._rc         = None
        self._retries    = 0
        self._fetch_pkg  = None
        self._start      = None

    def _advance(self, name):
        with self._lock:
            try:
                idx = STAGE_NAMES.index(name)
            except ValueError:
                return
            if idx > self._stage_idx:
                self._stage_idx = idx
                self._stage     = name

    def _ingest(self, raw):
        line = raw.rstrip()
        if not line:
            return
        self._captured.append(line)
        with self._lock:
            self._last_t = time.time()

        m = re.search(r"Downloading\s+([\w\-\.]+)", line, re.I)
        if m:
            with self._lock:
                self._fetch_pkg = m.group(1)

        if "retrying" in line.lower():
            with self._lock:
                self._retries += 1

        stage = _classify(line)
        if stage:
            self._advance(stage)

        if _is_script_output(line):
            self._script_out.append(line)

    def _bar_line(self, pulse, elapsed):
        with self._lock:
            stage     = self._stage
            idx       = self._stage_idx
            silence   = time.time() - self._last_t
            fetch_pkg = self._fetch_pkg
            retries   = self._retries

        running = (stage == "running")
        hanging = (silence > HANG_SECS)
        color   = STAGE_COLORS.get(stage, dim)

        frac   = _stage_frac(idx - 1) if running and idx > 0 else _stage_frac(idx)
        filled = int(BAR_WIDTH * frac)
        tip    = 1 if (filled < BAR_WIDTH and not running) else 0
        empty  = BAR_WIDTH - filled - tip

        bc  = amber if (running and hanging) else color
        bar = bc(BAR_FILLED * filled) + (bc(BAR_TIP) if tip else "") + faint(BAR_EMPTY * empty)

        t_str = f"{elapsed:5.1f}s"

        if running:
            pulse_str = dim(PULSE_FRAMES[pulse % len(PULSE_FRAMES)])
            label = (amber("waiting...") if hanging else green("running")) + f"  {pulse_str}"
        elif stage == "fetching" and fetch_pkg:
            label = amber(f"fetching {fetch_pkg}")
        else:
            label = color(stage)

        retry_str = dim(f" ↺{retries}") if retries else ""
        return f"  {bar}  {t_str}  {label}{retry_str}"

    def _final_line(self, elapsed, ok):
        t_str   = f"{elapsed:.2f}s"
        retries = self._retries
        if ok:
            bar   = green(BAR_FILLED * BAR_WIDTH)
            mark  = green("✓")
            state = dim(f"dissolved  {t_str}")
        else:
            with self._lock:
                idx = self._stage_idx
            n   = int(BAR_WIDTH * _stage_frac(idx))
            bar = red(BAR_FILLED * n) + faint(BAR_EMPTY * (BAR_WIDTH - n))
            mark  = red("✗")
            state = red(f"failed  {t_str}")
        retry_str = dim(f"  ↺{retries}") if retries else ""
        return f"  {bar}  {mark}  {state}{retry_str}"

    # ── Run modes ─────────────────────────────

    def run(self):
        if self.quiet:
            return subprocess.run(
                [sys.executable, str(self.engine_path)] + self.args
            ).returncode

        if not COLOR:
            print(f"  running {self.script_name}...")
            r = subprocess.run(
                [sys.executable, str(self.engine_path)] + self.args,
                capture_output=True, text=True,
            )
            if r.stdout: print(r.stdout, end="")
            if r.returncode != 0 and r.stderr:
                print(r.stderr, end="", file=sys.stderr)
            return r.returncode

        return self._run_live()

    def _run_live(self):
        cmd  = [sys.executable, str(self.engine_path)] + self.args
        self._start = time.time()
        _hide_cursor()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            def _read():
                for line in proc.stdout:
                    self._ingest(line)
                proc.wait()
                with self._lock:
                    self._done = True
                    self._rc   = proc.returncode

            t = threading.Thread(target=_read, daemon=True)
            t.start()

            sys.stdout.write("\n")
            pulse = 0
            while not self._done:
                elapsed = time.time() - self._start
                _clear_line()
                sys.stdout.write(self._bar_line(pulse, elapsed))
                sys.stdout.flush()
                pulse += 1
                time.sleep(0.05)

            t.join()
            elapsed = time.time() - self._start
            ok      = (self._rc == 0)
            _clear_line()
            sys.stdout.write(self._final_line(elapsed, ok) + "\n")
            sys.stdout.flush()

        finally:
            _show_cursor()

        # Script output
        if self._script_out:
            sys.stdout.write("\n")
            for line in self._script_out:
                sys.stdout.write(f"  {line}\n")
            sys.stdout.write("\n")

        # Error dump on failure
        if self._rc != 0:
            relevant = [l for l in self._captured
                        if l.strip() and not any(
                            c in l for c in ["│", "├", "└", "◉", "◎"])]
            if relevant:
                sys.stdout.write(f"\n  {dim('─' * 50)}\n\n")
                for line in relevant[-20:]:
                    if _is_error(line):
                        sys.stdout.write(f"  {red(line.strip())}\n")
                    else:
                        sys.stdout.write(f"  {dim(line.strip())}\n")
                sys.stdout.write("\n")

        return self._rc


# ─────────────────────────────────────────────
# Locate bubble.py
# ─────────────────────────────────────────────

def find_engine():
    for p in [
        Path(__file__).parent / "bubble.py",
        Path(os.environ.get("BUBBLE_ENGINE", "")),
    ]:
        if p and p.exists():
            return p
    found = shutil.which("bubble.py")
    return Path(found) if found else None

BUBBLE_ENGINE = find_engine()

AUTO_YES = False
QUIET    = False

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def out(msg="", end="\n"):
    if not QUIET:
        print(msg, end=end, flush=True)

def err(msg):
    print(msg, file=sys.stderr, flush=True)

def prompt_yn(q, default=True):
    if AUTO_YES:
        return True
    s = "Y/n" if default else "y/N"
    try:
        a = input(f"  {q} [{s}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        out(); return default
    return (a in ("y", "yes")) if a else default

def engine(args, capture=False):
    if not BUBBLE_ENGINE:
        err(f"  {red('✗')} bubble.py not found — set BUBBLE_ENGINE or place it alongside this script")
        sys.exit(1)
    cmd = [sys.executable, str(BUBBLE_ENGINE)] + args
    return subprocess.run(cmd, capture_output=True, text=True) if capture else subprocess.run(cmd)

def ensure_init():
    home = Path(os.environ.get("BUBBLE_HOME", Path.home() / ".bubble"))
    if (home / "vault.db").exists():
        return
    out()
    out(f"  {bold('◉ bubble')}  first run")
    out(f"\n  {dim('initializing ...')}", end=" ")
    r = engine(["vault", "list"], capture=True)
    out(green("done.") if r.returncode == 0 else red("failed."))
    if r.returncode != 0:
        err(r.stderr.strip()); sys.exit(1)
    out(f"\n  {dim('bubble <script.py>'):<36}  run a script")
    out(f"  {dim('bubble get <package>'):<36}  pre-cache a package")
    out(f"  {dim('bubble status'):<36}  vault contents")
    out(f"  {dim('bubble doctor'):<36}  diagnose")
    out()


# ─────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────

def cmd_run(script, extra_args, keep):
    sp = Path(script)
    if not sp.exists():
        err(f"  {red('✗')} not found: {script}"); sys.exit(1)

    if QUIET and AUTO_YES:
        args = ["up", script] + (["--keep"] if keep else []) + extra_args
        sys.exit(BubbleRunner(BUBBLE_ENGINE, args, quiet=True).run())

    out()
    out(f"  {dim('◎')} {cyan(sp.name)}")

    # Surface missing deps before running
    scan = engine(["scan", script, "--resolve"], capture=True)
    missing = []
    if scan.returncode == 0:
        for line in scan.stdout.splitlines():
            if ("✗" in line or "not in vault" in line.lower()) and line.strip():
                parts = re.sub(r"[✗\s]+", " ", line).strip().split()
                if parts:
                    missing.append(parts[0])

    if missing:
        out(f"  {amber('⚠')} missing: {', '.join(missing)}")
        if prompt_yn("Fetch from PyPI and cache?"):
            for pkg in missing:
                out(f"  {dim('◎')} fetching {amber(pkg)} ...")
                r = engine(["vault", "add", pkg, "--recursive"], capture=True)
                if r.returncode != 0:
                    out(f"  {dim('→')} could not pre-fetch {pkg}, will retry at runtime")

    args = ["up", script] + (["--keep"] if keep else []) + extra_args
    runner = BubbleRunner(BUBBLE_ENGINE, args, script_name=sp.name, quiet=QUIET)
    try:
        sys.exit(runner.run())
    except KeyboardInterrupt:
        _show_cursor(); out(f"\n  {amber('⚠')} interrupted"); sys.exit(130)


def cmd_get(packages):
    if not packages:
        err(f"  {red('✗')} specify at least one package"); sys.exit(1)
    out(f"\n  {dim('◎')} caching: {cyan(', '.join(packages))}\n")
    failed = []
    for pkg in packages:
        is_npm = pkg.startswith("npm:")
        raw    = pkg[4:] if is_npm else pkg
        flags  = ["--recursive"] + (["--npm"] if is_npm else [])
        runner = BubbleRunner(BUBBLE_ENGINE, ["vault", "add", raw] + flags,
                              script_name=raw, quiet=QUIET)
        try:
            rc = runner.run()
        except KeyboardInterrupt:
            _show_cursor(); out(); sys.exit(130)
        if rc != 0:
            failed.append(pkg)
    out()
    if failed:
        err(f"  {red('✗')} failed: {', '.join(failed)}"); sys.exit(1)
    out(f"  {green('◎')} done — packages are offline-safe\n")


def cmd_status():
    out()
    r = engine(["vault", "list"], capture=True)
    if r.returncode != 0:
        err(f"  {red('✗')} could not read vault"); sys.exit(1)
    lines = r.stdout.strip().splitlines()
    if not lines or "Vault is empty" in r.stdout:
        out(f"  {bold('◉ bubble')}  vault is empty")
        out(f"\n  {dim('bubble get <package>'):<36}  pre-cache a package\n")
        return
    out(f"  {bold('◉ bubble')}  vault\n")
    for line in lines:
        out(f"  {dim(line)}")
    home = Path(os.environ.get("BUBBLE_HOME", Path.home() / ".bubble"))
    bd   = home / "bubbles"
    if bd.exists():
        active = [d for d in bd.iterdir() if d.is_dir()]
        if active:
            out(f"\n  {amber('◎')} active bubbles: {len(active)}")
    out()


def cmd_doctor():
    out(); engine(["doctor"]); out()


def cmd_clean():
    out(f"\n  {dim('◎')} dissolving active bubbles ...", end=" ")
    r = engine(["down", "--all"], capture=True)
    out(green("done.") if r.returncode == 0 else "")
    if r.stdout.strip(): out(r.stdout.strip())
    out()


def cmd_preflight(script):
    sp = Path(script)
    if not sp.exists():
        err(f"  {red('✗')} not found: {script}"); sys.exit(1)
    out(); r = engine(["preflight", script]); out()
    sys.exit(r.returncode)


def cmd_default():
    ensure_init()
    home = Path(os.environ.get("BUBBLE_HOME", Path.home() / ".bubble"))
    out(f"\n  {bold('◉ bubble')}  {dim('v' + __version__)}\n")
    if (home / "vault.db").exists():
        r = engine(["vault", "list"], capture=True)
        for line in r.stdout.strip().splitlines():
            if "Total:" in line:
                out(f"  {dim(line.strip())}"); break
    out()
    out(f"  {cyan('bubble <script.py>'):<48}  run a script")
    out(f"  {cyan('bubble get <package> [...]'):<48}  pre-cache packages")
    out(f"  {cyan('bubble status'):<48}  vault contents")
    out(f"  {cyan('bubble doctor'):<48}  diagnose environment")
    out(f"  {cyan('bubble clean'):<48}  dissolve lingering bubbles")
    out(f"  {cyan('bubble preflight <script.py>'):<48}  offline readiness check")
    out(f"\n  {dim('flags:')}  {dim('--yes -y')}  auto-confirm  "
        f"  {dim('--quiet -q')}  agent mode  "
        f"  {dim('--keep')}  debug\n")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    global AUTO_YES, QUIET

    raw = sys.argv[1:]

    def pop(*flags):
        for f in flags:
            if f in raw:
                raw.remove(f); return True
        return False

    AUTO_YES = pop("--yes", "-y")
    QUIET    = pop("--quiet", "-q")
    keep     = pop("--keep")

    if pop("--version"):
        print(f"bubble-cli {__version__}"); sys.exit(0)

    if not raw:
        cmd_default(); return

    first = raw[0]

    dispatch = {
        "get":       lambda: cmd_get(raw[1:]),
        "status":    cmd_status,
        "doctor":    cmd_doctor,
        "clean":     cmd_clean,
        "--help":    cmd_default,
        "-h":        cmd_default,
        "help":      cmd_default,
    }

    if first in dispatch:
        dispatch[first]()
    elif first == "preflight":
        if len(raw) < 2:
            err("  Usage: bubble preflight <script.py>"); sys.exit(1)
        cmd_preflight(raw[1])
    else:
        ensure_init()
        cmd_run(first, extra_args=raw[1:], keep=keep)


if __name__ == "__main__":
    main()
