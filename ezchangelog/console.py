"""The pipeline's face: a live region that animates while work happens.

The bottom of the screen is owned by a ticking renderer -- stage map, spinner,
elapsed clock, token counter, and a rolling window of model output. Finished
work is logged *above* it and scrolls away normally, so the map never repeats
and the screen never looks frozen while a model is thinking.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from collections import deque
from typing import Callable

from . import ascii3d

_TTY = sys.stdout.isatty()

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PULSE = "▁▂▃▄▅▆▇▆▅▄▃▂"


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def dim(t: str) -> str:
    return _paint(t, "2")


def bold(t: str) -> str:
    return _paint(t, "1")


def cyan(t: str) -> str:
    return _paint(t, "36")


def green(t: str) -> str:
    return _paint(t, "32")


def red(t: str) -> str:
    return _paint(t, "31")


def yellow(t: str) -> str:
    return _paint(t, "33")


def blue(t: str) -> str:
    return _paint(t, "34")


def magenta(t: str) -> str:
    return _paint(t, "35")


_ANSI = __import__("re").compile(r"\033\[[0-9;?]*[A-Za-z]")


def _plain(text: str) -> str:
    """Text without colour codes -- needed to measure real column width."""
    return _ANSI.sub("", text)


def width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 24)).columns, 130))


BANNER = r"""
  ┌─┐┌─┐  ┌─┐┬ ┬┌─┐┌┐┌┌─┐┌─┐┬  ┌─┐┌─┐
  ├┤ ┌─┘  │  ├─┤├─┤││││ ┬├┤ │  │ ││ ┬
  └─┘└─┘  └─┘┴ ┴┴ ┴┘└┘└─┘└─┘┴─┘└─┘└─┘"""


class Live:
    """Owns the bottom N lines of the terminal and redraws them on a tick."""

    def __init__(self, render: Callable[[int], list[str]], fps: float = 12.0) -> None:
        self.render = render
        self.interval = 1.0 / fps
        self.enabled = _TTY
        self.drawn = 0
        self.tick = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        sys.stdout.write("\033[?25l")  # hide cursor: it flickers on redraw
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        with self._lock:
            self._erase()
            if self.enabled:
                sys.stdout.write("\033[?25h")  # show cursor
                sys.stdout.flush()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick += 1
            self.draw()
            self._stop.wait(self.interval)

    def _erase(self) -> None:
        if self.drawn and self.enabled:
            sys.stdout.write(f"\033[{self.drawn}A\033[J")
            self.drawn = 0

    def draw(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            lines = self.render(self.tick)
            self._erase()
            if lines:
                sys.stdout.write("\n".join(lines) + "\n")
                self.drawn = len(lines)
            sys.stdout.flush()

    def log(self, text: str) -> None:
        """Print above the live region, so finished work scrolls away."""
        with self._lock:
            self._erase()
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        self.draw()


class Console:
    def __init__(self, verbose: bool = True, stages: list[str] | None = None) -> None:
        self.verbose = verbose
        self.stages = stages or []
        self.done_stages: set[str] = set()
        self.current = ""
        self.detail_text = ""
        self.settings = ""
        self.stage_notes: dict[str, str] = {}
        self.stage_rows: list[str] = []
        self.started = time.time()
        self.stage_started = time.time()
        self.cost = 0.0
        self.calls = 0

        self._spin_a = 0.0
        self._spin_b = 0.0
        self._art_clock = time.time()

        self.streaming = False
        self.stream_lines: deque[str] = deque(maxlen=7)
        self.stream_buffer = ""
        self.stream_chars = 0
        self.stream_started = 0.0
        self.model_label = ""

        self.live = Live(self._render)

    # -- live region -------------------------------------------------------

    # The live region is one app frame:
    #     [ title ]
    #     [ stages + current work | model stream ]
    #     [ settings ]
    LEFT = 31
    ART = 28
    BODY = 13

    def _map_line(self, tick: int) -> str:
        cells = []
        for name in self.stages:
            if name in self.done_stages:
                cells.append(green(f"✔ {name}"))
            elif name == self.current:
                cells.append(bold(cyan(f"{SPINNER[tick % len(SPINNER)]} {name}")))
            else:
                cells.append(dim(f"· {name}"))
        return "  " + dim(" ─ ").join(cells)

    def _left(self, tick: int) -> list[str]:
        """Stage list, with the active stage's live rows beneath it."""
        rows: list[str] = []
        for name in self.stages:
            note = self.stage_notes.get(name, "")
            if name in self.done_stages:
                rows.append(f"{green('✔')} {name:<9}{dim(note)}")
            elif name == self.current:
                spin = SPINNER[tick % len(SPINNER)]
                clock = f"{time.time() - self.stage_started:4.1f}s"
                rows.append(f"{cyan(spin)} {bold(name):<18}{dim(clock)}")
                rows.append(f"  {dim(self.detail_text)}")
                shown = self.stage_rows[-5:]
                hidden = len(self.stage_rows) - len(shown)
                if hidden > 0:
                    rows.append(dim(f"  … {hidden} earlier"))
                rows += [f"  {r}" for r in shown]
            else:
                rows.append(dim(f"· {name}"))
        return rows

    def _right(self, tick: int, art: bool = True) -> list[str]:
        room = width() - self.LEFT - (self.ART + 2 if art else 0) - 7
        if not self.streaming:
            note = "  no model running"
            rows = []
            for i in range(self.BODY - 2):
                # Pad on the plain text: escape codes have no width on screen.
                text = note if i == 1 else ""
                rows.append(dim("│ ") + dim(text.ljust(room)) + dim(" │"))
            return (
                [dim("┌" + "─" * (room + 2) + "┐")]
                + rows
                + [dim("└" + "─" * (room + 2) + "┘")]
            )

        waited = time.time() - self.stream_started
        tokens = self.stream_chars // 4
        wave = "".join(PULSE[(tick + i * 2) % len(PULSE)] for i in range(5))
        label = (
            f" {self.model_label} · {tokens:,} tok · {waited:.1f}s "
            if tokens
            else f" {self.model_label} · thinking · {waited:.1f}s "
        )
        head = dim("┌─") + magenta(wave) + dim("─" + label.ljust(room - 7, "─") + "┐")

        body = list(self.stream_lines)
        if self.stream_buffer:
            body.append(self.stream_buffer)
        height = self.BODY - 2
        body = body[-height:]
        body = [""] * max(0, height - len(body)) + body

        out = [head]
        for line in body:
            text = line.replace("\t", "  ")
            if len(text) > room:
                text = text[-room:]
            out.append(dim("│ ") + text.ljust(room) + dim(" │"))
        out.append(dim("└" + "─" * (room + 2) + "┘"))
        return out

    def _art(self) -> list[str]:
        """A rotating 3D solid that doubles as a progress read-out.

        It spins faster while a model is streaming, and the torus closes up as
        stages complete -- so a glance tells you both that work is alive and
        roughly how far along it is.
        """
        now = time.time()
        speed = 2.4 if self.streaming else 0.9
        delta = min(0.2, now - self._art_clock)
        self._art_clock = now
        self._spin_a += delta * speed * 0.9
        self._spin_b += delta * speed * 0.5

        done = len(self.done_stages)
        progress = done / max(1, len(self.stages))
        height = self.BODY
        try:
            if done == 0:
                rows = ascii3d.sphere(self.ART, height, self._spin_a, self._spin_b)
            else:
                rows = ascii3d.torus(
                    self.ART, height, self._spin_a, self._spin_b,
                    r1=1.0, r2=2.6 - 1.1 * progress,
                )
        except Exception:  # never let decoration break the run
            return [""] * height
        colour = green if progress > 0.75 else cyan if self.streaming else blue
        return [colour(r) for r in rows]

    def _render(self, tick: int) -> list[str]:
        if not self.current:
            return []
        total = width()
        rule = dim("─" * (total - 2))
        show_art = total >= 104

        columns = [self._left(tick)]
        if show_art:
            columns.append(self._art())
        columns.append(self._right(tick, art=show_art))

        height = max(max(len(c) for c in columns), self.BODY)
        columns = [c + [""] * (height - len(c)) for c in columns]
        widths = [self.LEFT] + ([self.ART + 2] if show_art else [])

        lines = [f"  {rule}"]
        for row in zip(*columns):
            out = "  "
            for index, cell in enumerate(row[:-1]):
                plain = _plain(cell)
                limit = widths[index]
                if len(plain) > limit - 1:
                    cell, pad = plain[: limit - 2] + "…", 1
                else:
                    pad = limit - len(plain)
                out += cell + " " * max(1, pad)
            lines.append(out + row[-1])
        lines.append(f"  {rule}")
        lines.append("  " + dim(self.settings))
        return lines

    # -- chrome ------------------------------------------------------------

    def banner(self, subtitle: str, settings: str = "") -> None:
        print(cyan(BANNER) if _TTY else "ez-changelog")
        print(dim(f"  {subtitle}"))
        self.settings = settings
        self.live.start()

    def stage(self, name: str, detail: str = "") -> None:
        if self.current:
            self.done_stages.add(self.current)
        self.current = name
        self.detail_text = detail
        self.stage_rows = []
        self.stage_started = time.time()

    def step(self, message: str) -> None:
        """A row inside the current stage's panel -- live, not scrollback."""
        self.stage_rows.append(message)

    def detail(self, message: str) -> None:
        if self.verbose:
            self.stage_rows.append(dim(message))

    def bar(self, label: str, value: float, note: str = "", cells: int = 12) -> None:
        value = max(0.0, min(1.0, value))
        filled = round(value * cells)
        art = (
            blue("█" * filled) + dim("░" * (cells - filled))
            if _TTY
            else "#" * filled + "." * (cells - filled)
        )
        self.stage_rows.append(f"{label:<19}{art} {note}")

    def done(self, message: str = "") -> None:
        elapsed = time.time() - self.stage_started
        self.stage_notes[self.current] = f"{message}  {elapsed:.1f}s".strip()

    def warn(self, message: str) -> None:
        self.live.log(f"  {yellow('!')} {message}")

    def error(self, message: str) -> None:
        self.live.log(f"  {red('✖')} {message}")

    def model(self, model: str, effort: str, purpose: str) -> None:
        self.calls += 1
        self.model_label = f"{model}/{effort}"
        self.stage_rows.append(dim(f"◆ {purpose}"))

    # -- streaming ---------------------------------------------------------

    def stream_open(self, height: int = 7) -> None:
        self.stream_lines = deque(maxlen=height)
        self.stream_buffer = ""
        self.stream_chars = 0
        self.stream_started = time.time()
        self.streaming = True

    def stream_text(self, piece: str) -> None:
        self.stream_chars += len(piece)
        if not self.verbose:
            return
        self.stream_buffer += piece
        while "\n" in self.stream_buffer:
            line, _, self.stream_buffer = self.stream_buffer.partition("\n")
            self.stream_lines.append(line)

    def stream_close(self, reply=None) -> None:
        self.streaming = False
        self.stream_buffer = ""
        self.stream_lines.clear()
        if reply is not None:
            self.cost += reply.cost_usd
            self.stage_rows.append(
                dim(
                    f"  {reply.input_tokens/1000:.0f}k in · {reply.output_tokens:,} out"
                    f" · {reply.cost} · {reply.duration_ms/1000:.0f}s"
                )
            )

    # -- finish ------------------------------------------------------------

    def finish(self, path: str, counts: dict[str, int]) -> None:
        if self.current:
            self.done_stages.add(self.current)
        self.current = ""
        self.live.stop()

        elapsed = time.time() - self.started
        print()
        print(self._map_line(0))
        for name in self.stages:
            note = self.stage_notes.get(name)
            if note:
                print(f"    {green('✔')} {name:<9}{dim(note)}")
        inner = width() - 4
        print()
        print("  " + dim("╭" + "─" * inner + "╮"))

        def row(plain: str, painted: str | None = None) -> None:
            pad = " " * max(0, inner - len(plain) - 2)
            print("  " + dim("│") + " " + (painted or plain) + pad + " " + dim("│"))

        row("JOURNAL READY", bold(green("JOURNAL READY")))
        row("")
        stat = "   ".join(f"{v} {k}" for k, v in counts.items())
        row(stat, dim(stat))
        spend = f"{elapsed:.0f}s   {self.calls} model calls   ${self.cost:.2f}"
        row(spend, dim(spend))
        print("  " + dim("╰" + "─" * inner + "╯"))
        print(f"\n  {bold(path)}\n", flush=True)

    def abort(self) -> None:
        """Tear down the live region, leaving the results on screen.

        The live frame is erased on stop, so anything worth keeping has to be
        reprinted -- otherwise a stopped run ends on a blank screen.
        """
        self.live.stop()
        if self.stage_notes:
            print()
            for name in self.stages:
                note = self.stage_notes.get(name)
                if note:
                    print(f"    {green('✔')} {name:<9}{dim(note)}")
            print()
