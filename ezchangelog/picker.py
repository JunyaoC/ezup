"""Full-screen checkbox picker for choosing sessions.

Space toggles, Enter confirms, q aborts. Falls back to a numbered prompt when
there is no terminal (see ``cli.interactive_chooser``).
"""

from __future__ import annotations

import curses
from typing import Any, Sequence

ABORTED = object()

_ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
_ESCAPE = 27
_SPACE = 32


def _fmt_ts(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ").replace("Z", "")[:16]


def _clip(text: str, room: int) -> str:
    if room <= 0:
        return ""
    if len(text) <= room:
        return text.ljust(room)
    return text[: room - 1] + "…"


def _columns(selections: Sequence[Any]) -> tuple[bool, bool]:
    """Which optional columns this run needs: (synced, author).

    Decided once for the whole list rather than per row, so a column that is
    empty on some sessions still keeps every row aligned.
    """
    return (
        any(getattr(s, "synced", "") for s in selections),
        any(getattr(s, "author", "") for s in selections),
    )


def _row_text(
    selection: Any, checked: bool, width: int, columns: tuple[bool, bool] = (False, False)
) -> str:
    facts = selection.facts
    tools = sum(facts.tool_uses.values())
    box = "[x]" if checked else "[ ]"
    days = f" +{len(selection.window_days) - 1}d" if len(selection.window_days) > 1 else ""
    show_sync, show_author = columns
    # `getattr` rather than attribute access: the picker stays usable with any
    # row object, not only the Selection dataclass.
    synced = str(getattr(selection, "synced", "") or "-")
    who = str(getattr(selection, "author", "") or "me")
    prefix = (
        f"{box} {selection.last_active + days:<17}"
        + (f"{_clip(synced, 11)}" if show_sync else "")
        + f"{selection.match_reason:<7}"
        + (f"{_clip(who, 13)}" if show_author else "")
        + f"{facts.user_turns:>5}t {tools:>5}x  "
    )
    # Split the leftover width between the directory and the title.
    room = max(0, width - len(prefix) - 3)
    dir_room = max(12, min(34, room // 2))
    title_room = max(0, room - dir_room)
    directory = _clip(selection.display_dir, dir_room)
    title = _clip(facts.title or facts.session_id[:8], title_room)
    return f"{prefix}{directory}  {title}"[: width - 1]


def _draw(
    screen: "curses._CursesWindow",
    selections: Sequence[Any],
    checked: list[bool],
    cursor: int,
    top: int,
    body_height: int,
    columns: tuple[bool, bool] = (False, False),
    verb: str = "collect",
) -> None:
    height, width = screen.getmaxyx()
    screen.erase()

    count = sum(checked)
    header = (
        f" {len(selections)} sessions   {count} picked   "
        f"[space] toggle  [a] all  [n] none  [enter] {verb}  [q] cancel"
    )
    screen.addnstr(0, 0, header.ljust(width - 1), width - 1, curses.A_REVERSE)

    for offset in range(body_height):
        index = top + offset
        if index >= len(selections):
            break
        text = _row_text(selections[index], checked[index], width, columns)
        attribute = curses.A_BOLD if index == cursor else curses.A_NORMAL
        screen.addnstr(offset + 1, 0, text.ljust(width - 1), width - 1, attribute)
        if index == cursor:
            screen.chgat(offset + 1, 0, width - 1, curses.A_REVERSE)

    if len(selections) > body_height:
        position = f" {cursor + 1}/{len(selections)} "
        screen.addnstr(height - 1, 0, position.rjust(width - 1), width - 1, curses.A_DIM)
    screen.refresh()


def _run(
    screen: "curses._CursesWindow", selections: Sequence[Any], verb: str = "collect"
) -> list[int] | object:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)

    columns = _columns(selections)
    # Nothing is ticked to begin with: the picker is the consent step, so
    # confirming without touching anything must be a no-op.
    checked = [False] * len(selections)
    cursor = 0
    top = 0

    while True:
        height, _ = screen.getmaxyx()
        body_height = max(1, height - 2)

        # Keep the cursor inside the viewport.
        if cursor < top:
            top = cursor
        elif cursor >= top + body_height:
            top = cursor - body_height + 1
        top = max(0, min(top, max(0, len(selections) - body_height)))

        _draw(screen, selections, checked, cursor, top, body_height, columns, verb)

        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return ABORTED

        if key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(selections) - 1, cursor + 1)
        elif key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_NPAGE:
            cursor = min(len(selections) - 1, cursor + body_height)
        elif key == curses.KEY_PPAGE:
            cursor = max(0, cursor - body_height)
        elif key in (curses.KEY_HOME, ord("g")):
            cursor = 0
        elif key in (curses.KEY_END, ord("G")):
            cursor = len(selections) - 1
        elif key == _SPACE:
            checked[cursor] = not checked[cursor]
            cursor = min(len(selections) - 1, cursor + 1)
        elif key == ord("a"):
            checked = [True] * len(selections)
        elif key == ord("n"):
            checked = [False] * len(selections)
        elif key in _ENTER_KEYS:
            return [i for i, on in enumerate(checked) if on]
        elif key in (ord("q"), _ESCAPE):
            return ABORTED
        elif key == curses.KEY_RESIZE:
            continue


def pick(selections: Sequence[Any], verb: str = "collect") -> list[Any] | object:
    """Run the picker. Returns the chosen selections, or ABORTED if cancelled.

    ``verb`` names what Enter will do, because the same picker now confirms two
    very different actions: collecting locally, and sharing off the machine.
    """
    if not selections:
        return []
    chosen = curses.wrapper(_run, selections, verb)
    if chosen is ABORTED:
        return ABORTED
    return [selections[index] for index in chosen]
