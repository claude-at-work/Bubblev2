"""term — color, cursor, and honest progress for the human face.

The doctrine, kept from legacy/bubble_cli.py:142-149:

  - Known stages advance the bar to real milestones.
  - The active leaf stage pulses — no fake forward motion.
  - Silence past HANG_SECS turns the label amber so the human knows it's
    slow, not broken.
  - Failure prints clean below the bar; success prints a one-line ✓.

Color/quiet state is read from env at use time so the CLI can flip
BUBBLE_COLOR / BUBBLE_QUIET before any rendering happens.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional, Sequence


# ─── color / quiet detection ───────────────────────────────────────────


def is_color() -> bool:
    if os.environ.get("BUBBLE_COLOR") == "1":
        return True
    if os.environ.get("BUBBLE_COLOR") == "0" or os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    return os.environ.get("TERM", "") != "dumb"


def is_quiet() -> bool:
    return bool(os.environ.get("BUBBLE_QUIET"))


def is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _wrap(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if is_color() else text


def dim(t: str) -> str:   return _wrap("2", t)
def green(t: str) -> str: return _wrap("32", t)
def amber(t: str) -> str: return _wrap("33", t)
def red(t: str) -> str:   return _wrap("31", t)
def cyan(t: str) -> str:  return _wrap("36", t)
def bold(t: str) -> str:  return _wrap("1", t)
def faint(t: str) -> str: return _wrap("2;37", t)


# ─── cursor / line / output helpers ────────────────────────────────────


def hide_cursor() -> None:
    if is_color() and not is_quiet():
        sys.stdout.write("\033[?25l"); sys.stdout.flush()


def show_cursor() -> None:
    if is_color() and not is_quiet():
        sys.stdout.write("\033[?25h"); sys.stdout.flush()


def clear_line() -> None:
    if is_color() and not is_quiet():
        sys.stdout.write("\r\033[2K")
    else:
        sys.stdout.write("\r")
    sys.stdout.flush()


def out(msg: str = "", end: str = "\n") -> None:
    """Like print, but honors BUBBLE_QUIET."""
    if not is_quiet():
        sys.stdout.write(msg + end); sys.stdout.flush()


def err(msg: str) -> None:
    """Always prints — errors are not silenced by quiet mode."""
    sys.stderr.write(msg + "\n"); sys.stderr.flush()


# ─── progress ──────────────────────────────────────────────────────────


BAR_WIDTH    = 28
PULSE_FRAMES = ["·  ", "·· ", "···", " ··", "  ·", "   "]
BAR_FILLED   = "█"
BAR_TIP      = "▓"
BAR_EMPTY    = "░"
HANG_SECS    = 8.0


# Stage → color. Extend by mutating, or pass a custom map to Progress.
STAGE_COLOR = {
    "scanning":    cyan,
    "probing":     cyan,
    "resolving":   cyan,
    "fetching":    amber,
    "downloading": amber,
    "unpacking":   cyan,
    "hashing":     cyan,
    "indexing":    cyan,
    "importing":   cyan,
    "linking":     cyan,
    "verifying":   cyan,
    "assembling":  cyan,
    "running":     green,
    "dissolving":  dim,
}


class Progress:
    """An honest progress bar driven by explicit stage transitions.

        with Progress(["scanning", "fetching", "running"]) as p:
            p.stage("scanning"); ...
            p.stage("fetching"); p.detail("requests==2.33.1")
            p.stage("running"); ...

    The last stage in the list is the active leaf — it pulses rather than
    fakes progress. Override with `active_stage=`.

    On context exit, prints a single ✓ + elapsed (or ✗ if an exception
    bubbled out, or `p.fail()` was called).

    If TTY/color is unavailable, the bar is silent — but `stage()` and
    `detail()` are still safe to call.
    """

    def __init__(
        self,
        stages: Sequence[str],
        weights: Optional[Sequence[int]] = None,
        active_stage: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        self.stages = list(stages) or ["working"]
        self.weights = list(weights) if weights else [1] * len(self.stages)
        self.total_weight = sum(self.weights) or 1
        self.active_stage = active_stage or self.stages[-1]
        self.title = title

        self._idx = 0
        self._stage_name = self.stages[0]
        self._detail = ""
        self._lock = threading.Lock()
        self._last_t = time.time()
        self._start = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._final_status: Optional[str] = None  # "ok" | "fail"
        self._final_message: str = ""
        self._enabled = is_color() and not is_quiet()

    # ── public API ──────────────────────────────────────────────────────

    def stage(self, name: str, detail: str = "") -> None:
        with self._lock:
            try:
                idx = self.stages.index(name)
            except ValueError:
                return
            if idx >= self._idx:
                self._idx = idx
                self._stage_name = name
                self._detail = detail
                self._last_t = time.time()

    def detail(self, text: str) -> None:
        with self._lock:
            self._detail = text
            self._last_t = time.time()

    def fail(self, message: str = "") -> None:
        self._final_status = "fail"
        self._final_message = message

    def succeed(self, message: str = "") -> None:
        self._final_status = "ok"
        self._final_message = message

    # ── context manager ─────────────────────────────────────────────────

    def __enter__(self) -> "Progress":
        if self._enabled:
            self._start = time.time()
            hide_cursor()
            if self.title:
                sys.stdout.write(f"\n  {dim('◎')} {cyan(self.title)}\n")
            self._thread = threading.Thread(target=self._render_loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        if self._enabled:
            elapsed = time.time() - self._start
            ok = (exc_type is None) and (self._final_status != "fail")
            clear_line()
            sys.stdout.write(self._final_line(elapsed, ok) + "\n")
            sys.stdout.flush()
            show_cursor()

    # ── render ──────────────────────────────────────────────────────────

    def _frac(self) -> float:
        with self._lock:
            idx = self._idx
            running_leaf = (self.stages[idx] == self.active_stage)
        completed = sum(self.weights[:idx])
        if not running_leaf:
            completed += self.weights[idx]
        return min(completed / self.total_weight, 1.0)

    def _bar(self, frac: float, color_fn, running: bool) -> str:
        filled = int(BAR_WIDTH * frac)
        tip = 1 if (filled < BAR_WIDTH and not running) else 0
        empty = BAR_WIDTH - filled - tip
        return (color_fn(BAR_FILLED * filled)
                + (color_fn(BAR_TIP) if tip else "")
                + faint(BAR_EMPTY * empty))

    def _render_loop(self) -> None:
        pulse = 0
        while not self._stop.is_set():
            with self._lock:
                stage = self._stage_name
                detail = self._detail
                last_t = self._last_t
            elapsed = time.time() - self._start
            silence = time.time() - last_t
            running = (stage == self.active_stage)
            hanging = (silence > HANG_SECS)

            color_fn = STAGE_COLOR.get(stage, dim)
            bar_color = amber if (running and hanging) else color_fn
            bar = self._bar(self._frac(), bar_color, running)

            if running:
                pulse_str = dim(PULSE_FRAMES[pulse % len(PULSE_FRAMES)])
                if hanging:
                    label = amber("waiting...") + f"  {pulse_str}"
                elif detail:
                    label = color_fn(stage) + f"  {dim(detail)}  {pulse_str}"
                else:
                    label = color_fn(stage) + f"  {pulse_str}"
            elif detail:
                label = color_fn(f"{stage}  ") + dim(detail)
            else:
                label = color_fn(stage)

            t_str = f"{elapsed:5.1f}s"
            clear_line()
            sys.stdout.write(f"  {bar}  {t_str}  {label}")
            sys.stdout.flush()
            pulse += 1
            time.sleep(0.05)

    def _final_line(self, elapsed: float, ok: bool) -> str:
        t_str = f"{elapsed:.2f}s"
        if ok:
            bar = green(BAR_FILLED * BAR_WIDTH)
            mark = green("✓")
            state = dim(f"{self._final_message or 'done'}  {t_str}")
        else:
            n = int(BAR_WIDTH * self._frac())
            bar = red(BAR_FILLED * n) + faint(BAR_EMPTY * (BAR_WIDTH - n))
            mark = red("✗")
            label = self._final_message or "failed"
            state = red(f"{label}  {t_str}")
        return f"  {bar}  {mark}  {state}"


# ─── prompts ───────────────────────────────────────────────────────────


def prompt_yn(question: str, default: bool = True, auto_yes: Optional[bool] = None) -> bool:
    """Yes/no prompt. Honors --yes via BUBBLE_AUTOYES env. EOF/interrupt → default.
    Non-TTY also yields default (so bubble run inside a script doesn't hang)."""
    if auto_yes is None:
        auto_yes = bool(os.environ.get("BUBBLE_AUTOYES"))
    if auto_yes:
        return True
    if not is_tty():
        return default
    s = "Y/n" if default else "y/N"
    try:
        a = input(f"  {question} [{s}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        out()
        return default
    return (a in ("y", "yes")) if a else default
