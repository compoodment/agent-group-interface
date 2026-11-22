"""The input box: keys, paste, chips, history, completion and commands. Owner: Claude."""

import curses
import difflib
import functools
import os
import re
import select
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from agi import (__version__, backup, connect, delivery_probe, gate_reminders, installation, membership, store,
                 todo as todo_state, ui_state)
from agi.ui import clipboard, pixbuf, sound, when
from agi.ui import keys as modified
from agi.ui.shiftkey import shift_held
from agi.ui_state import CHIP_FIRST, is_chip
from agi.ui.text import (REPLY, cell_width, clean, clusters, duration, fit, held_over_a_day, helpers_tag,
                         parse_duration, text_width, waiting_for)


QUIT_WINDOW = 2             # seconds for the second Ctrl+C
MENU_ROWS = 6               # commands shown at once in the / menu
COMMAND_HELP = {            # the / menu: command -> (argument hint, what it does); order is menu order
    "/reply": ("N text", "answer message N · Tab picks a reply"),
    "/find": ("words [from:codex]", "search messages: the newest, and a list of older ones"),
    "/catchup": ("", "what's waiting for you since you last read"),
    "/export": ("[A-B]", "save the chat as Markdown"),
    "/custom": ("N M [D]", "set a custom posting rate or reset with off"),
    "/mode": ("normal | wait", "default rate, or wait for you"), "/pause": ("", "stop agent posts"),
    "/resume": ("", "let agents post again"), "/who": ("", "agent status and response times · /who test claude checks delivery"),
    "/mouse": ("on | off", "scroll the chat, or select text normally"),
    "/sound": ("away | always | off", "a sound when you're mentioned"),
    "/claim": ("path", "claim a shared file"), "/release": ("path", "release a claimed file"),
    "/claims": ("", "list claimed files"), "/tasks": ("", "requests in hand, and the latest done"),
    "/connect": ("", "set up Claude Code or Codex for this chat"),
    "/join": ("claude | codex", "deliver the chat to that agent again"),
    "/leave": ("claude | codex", "stop delivering to that agent; its claims stay"),
    "/unclaim": ("#N", "take an agent's claim off request N"),
    "/todo": ("[add what when | #N 30m | #N gate PROJECT SHA | #N off]",
              "to-dos, one of your own, or a check on a request"),
    "/gates": ("", "integration locks and waiting branches"),
    "/whatsnew": ("", "what's new in this version"),
    "/settings": ("", "sound, notifications, mouse, agents' posting"),
    "/update": ("", "check for a newer AGI and install it"),
    "/backup": ("", "save a copy of this chat as a ZIP"),
    "/decisions": ("", "your answers to marked questions"),
    "/help": ("", "keys and commands"), "/quit": ("", "leave"),
}
ARGUMENT_CHOICES = {
    "/mode": (("normal", "default posting rate"), ("wait", "wait for your next message")),
    "/mouse": (("on", "scroll the chat with the mouse"), ("off", "select text normally")),
    "/sound": (("away", "only while you're not looking at AGI"), ("always", "also while AGI is in front"),
               ("off", "no sound")),
    "/custom": (("off", "return to the default posting rate"),),
    "/connect": (("claude", "Claude Code"), ("codex", "Codex")),
    "/who": (("test claude", "check that messages reach Claude's session"),),
    "/join": (("claude", "Claude's session"), ("codex", "Codex's session")),
    "/leave": (("claude", "Claude's session"), ("codex", "Codex's session")),
}
START_LINE = {"claude": "start Claude with `claude --dangerously-load-development-channels server:agi` and say "
                        "\"join the chat\"",
              "codex": "start Codex and say \"join the chat\""}
def codex_resume(agent, session=None):
    """Codex rejoins through its bridge, which also starts the watcher for the chosen session (Codex, #6026)."""
    from agi import bridge
    return bridge.resume(session=session)


def timestamp(value):
    """A store time as Unix seconds: to-dos keep ISO times (created_at, updated_at) and Unix ones (due_at)."""
    return float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value).timestamp()


def since_days(value):
    """How long ago a store time was, for to-dos that last days: "40m", "5h", "3d", "3d 2h"."""
    seconds = max(0, int(time.time() - timestamp(value)))
    if seconds < 3600:
        return duration(seconds)
    hours = seconds // 3600
    return f"{hours}h" if hours < 24 else f"{hours // 24}d" + (f" {hours % 24}h" if hours % 24 else "")


WHEEL_ROWS = 3              # rows per touchpad/wheel step
DRAG_ON, DRAG_OFF = "\x1b[?1002h", "\x1b[?1002l"   # motion reports while a button is held: a drag
UNTIL_KEY = float("inf")    # a note's lifetime for lists you read (/claims, /who, /tasks): no time limit, and the next
WAKE_LATE = 180             # /who: a message of yours unhandled this long (seconds) says whether a wake was queued;
                            # above the 120 s a watcher may hold a quiet message to batch a burst
NAMES = ("@claude", "@codex", "@all")
COMMANDS = tuple(COMMAND_HELP)
PASTE_START, PASTE_END = "[200~", "[201~"
PASTE_IDLE = 2.0             # a terminal sends a paste in one burst: this long without its end marker ends it
REPLY_DRAFT = re.compile(r"/reply(?:\s+#?(\d+))?\s*")   # what Tab fills in /reply's number for
CHIP_CHARS, CHIP_LINES = 1000, 12    # larger pastes show as a chip and expand on send
# A numbered answer starts with a number and one letter, followed by the end of the line, space or punctuation.
ANSWER = re.compile(r"([ \t]*)(\d{1,2})[a-zA-Z](?=[ \t,.;:!?)]|\Z)")


def answer_run(lines):
    """The last number of the answers 1, 2, … that end the given lines without a gap, or 0. It walks up from the
    nearest line and stops at the first 1, so "2b" alone or after a line that isn't its 1 is no run, and a new run
    after an earlier one counts on its own."""
    last = expected = None
    for line in reversed(lines):
        found = ANSWER.match(line)
        if not found:
            return 0
        number = int(found.group(2))
        if last is None:
            last = number
        elif number != expected:
            return 0
        if number == 1:
            return last
        expected = number - 1
    return 0


LIST_ITEM = re.compile(r"([ \t]*)(?:(\d{1,9})([.)])(?:[ \t]+|(?=\n|\Z))|([-*\u2022])[ \t]+)")   # "4. ", "4.", "4) ", "- "
HISTORY_KEYS = {curses.KEY_SR: -1, curses.KEY_SF: 1,    # Shift+Up/Down as terminfo names them
                b"kUP": -1, b"kDN": 1, b"kUP3": -1, b"kDN3": 1, b"kUP5": -1, b"kDN5": 1}   # and Shift/Alt/Ctrl, extended


@dataclass(frozen=True, slots=True)
class MenuChoice:
    draft: str
    title: str
    description: str
    runnable: bool


class Composer:
    """The draft: keys, bracketed paste, chips, history, completion and sending. Mixed into Screen."""
    confirming = None           # a yes/no in the note line: (on yes, on no); set by ask()
    pick = None                 # the chosen row of a /find or /catchup list: ↑↓ move, Enter opens it
    pick_from = 0               # the list's first row (notes above it, like /find's own jump note, aren't in it)
    background = None           # (then, result) from a /update or /backup thread, for the main loop to take
    row_actions = {}            # /settings: what Enter does on each chosen row (instead of opening a link)
    notify_on = True            # desktop notifications, from the saved settings (/settings)
    kept_draft = None           # a message you were typing when Ctrl+A started a to-do: back once it's done
    command_place = None        # where you were reading when you typed the / of a command, before its menu
    restart_with = None         # after /update: the launcher to start once the screen has closed cleanly

    images = stash_images = ()      # the draft's images; Screen sets its own lists, never changed in place
    attaching = 0

    def composer_lines(self, width):
        """Visual rows of the draft, chips drawn as their labels, and the cursor's (row, col)."""
        rows, _starts, cursor = self._composer_layout(width)
        if self.chips:
            rows = ["".join(fit(self.chip_label(ch), width - 2) if ch in self.chips else ch for ch in row)
                    for row in rows]
        return rows, cursor

    def _cells(self, ch, width):
        """A draft character's width on screen: a chip is as wide as its label (at most a row)."""
        return min(text_width(self.chip_label(ch)), width - 2) if ch in self.chips else cell_width(ch)

    def _composer_layout(self, width):
        """Visual rows, the draft offset each row starts at, and the cursor's (row, col)."""
        room = width - 2
        rows, starts, cursor = [], [], None
        position = 0
        for line in self.draft.split("\n"):
            chunk, used, start = "", 0, position
            row_start = position
            for offset, ch in enumerate(line + "\0"):
                if offset == len(line):
                    if position + offset == self.cursor and cursor is None:
                        if used >= room:    # a full row leaves no cell for the cursor: it starts the next
                            rows.append(chunk)
                            starts.append(row_start)
                            chunk, used, row_start = "", 0, position + offset
                        cursor = (len(rows), used)
                    break
                size = self._cells(ch, width)
                if used + size > room:
                    rows.append(chunk)
                    starts.append(row_start)
                    chunk, used, row_start = "", 0, position + offset
                if position + offset == self.cursor and cursor is None:
                    cursor = (len(rows), used)
                chunk += ch
                used += size
            rows.append(chunk)
            starts.append(row_start)
            position = start + len(line) + 1
        return rows, starts, cursor or (len(rows) - 1, 0)


    # -- input ------------------------------------------------------------

    def read_keys(self):
        while True:
            try:
                self.pending.append(self.read_key())
            except curses.error:
                break

    def read_key(self):
        """One key; a mouse event's details taken at once: ncurses keeps only eight, so a fast flick read after
        draining the keys lost most of its wheel steps (the sweep after #3342: 200 steps scrolled 5 messages)."""
        key = self.win.get_wch()
        if key == curses.KEY_MOUSE:
            try:
                self.mouse_events.append(curses.getmouse())
            except curses.error:
                self.mouse_events.append(None)      # one per KEY_MOUSE, so each key finds its own
        return key

    def handle_pending(self):
        """Handle buffered keys. Returns False to quit."""
        keys = self.pending
        index = 0
        while index < len(keys):
            key = keys[index]
            if self.pasting is not None and time.monotonic() - self.paste_at > PASTE_IDLE:
                self.paste("".join(self.pasting).replace("\r\n", "\n").replace("\r", "\n"))
                self.pasting = None          # its end marker never came: what arrived is the paste, and this
                                             # key (and Ctrl+C) acts normally again (UI bug hunt, 2026-11-18)
            if self.pasting is not None:
                if key == "\x1b" and "".join(str(k) for k in keys[index + 1:index + 6]) == PASTE_END:
                    self.paste("".join(self.pasting).replace("\r\n", "\n").replace("\r", "\n"))
                    self.interacted = True
                    self.pasting = None
                    index += 6
                    continue
                if key == "\x1b" and len(keys) - index < 6:
                    break                   # wait for the rest of a possible end marker
                if isinstance(key, str):
                    self.pasting.append(key)
                    self.paste_at = time.monotonic()
                index += 1
                continue
            if key == "\x1b":
                rest = keys[index + 1:]
                started = "".join(k for k in rest[:5] if isinstance(k, str))
                if len(rest) < 5 and PASTE_START.startswith(started):
                    if self.more_input():
                        keys = self.pending = keys + self.pending_tail
                        continue            # a marker split across reads: look again with the rest
                    if started:
                        break               # "ESC[2..." so far: keep it until the rest of the marker arrives
                if "".join(str(k) for k in rest[:5]) == PASTE_START:
                    self.pasting, self.paste_at = [], time.monotonic()
                    index += 6
                    continue
                alt = self.alt_keys().get(rest[0]) if rest else None
                if alt:                     # Alt+Enter new line, Alt+B/F word moves, Alt+Backspace delete word
                    alt()
                    self.interacted = True
                    index += 2
                    continue
                if rest and rest[0] == "]":         # a late OSC reply (e.g. the colour query): swallow it
                    end = index + 2
                    while end < len(keys) and keys[end] != "\x07" and not (keys[end] == "\\" and keys[end - 1] == "\x1b"):
                        end += 1
                    if end >= len(keys):
                        break                       # wait for the terminator
                    index = end + 1
                    continue
                if rest[:2] in (["[", "I"], ["[", "O"]):
                    self.focus(rest[1] == "I")      # raw focus report from the terminal
                    index += 3
                    continue
                ss3 = rest[:1] == ["O"] and len(rest) > 1 and rest[1] in ("A", "B", "C", "D", "H", "F")
                if rest and (rest[0] == "[" or ss3):    # CSI, or SS3 cursor keys curses didn't decode
                    end = index + 2
                    while not ss3 and end < len(keys) and not (isinstance(keys[end], str) and "@" <= keys[end] <= "~"):
                        end += 1
                    if end >= len(keys) and self.more_input():
                        keys = self.pending = keys + self.pending_tail
                        continue            # a report split across reads: look again with the rest
                    decoded = modified.decode("".join(str(k) for k in keys[index + 1:end + 1]))
                    index = end + 1         # an unknown escape sequence is ignored
                    if decoded is None:
                        continue
                    self.interacted = True
                    if decoded[0] == "newline":         # Shift/Alt/Ctrl+Enter from a terminal that tells them apart
                        self.newline()
                    elif decoded[0] == "escape":
                        self.escape()
                    elif decoded[0] == "alt":
                        action = self.alt_keys().get(decoded[1])
                        if action:
                            action()
                    elif not self.key(decoded[1]):
                        self.pending = []
                        return False
                    continue
                self.interacted = True      # before: closing a list says it wasn't reading (#6113)
                self.escape()
                index += 1
                continue
            if not self.key(key):
                self.pending = []
                return False
            index += 1
        self.pending = keys[index:]
        return True

    def alt_keys(self):
        newline = self.newline
        return {"\r": newline, "\n": newline, curses.KEY_ENTER: newline, "b": self.word_left,
                "f": self.word_right, "\x7f": self.delete_word, curses.KEY_BACKSPACE: self.delete_word}

    def more_input(self):
        """Wait briefly for the rest of an escape sequence. True if more keys arrived."""
        self.pending_tail = []
        if not select.select([sys.stdin], [], [], 0.03)[0]:
            return False
        while True:
            try:
                self.pending_tail.append(self.read_key())
            except curses.error:
                break
        return bool(self.pending_tail)

    def pointer(self):
        """A mouse event: the wheel scrolls; a press on the transcript starts a selection, which a drag to another
        cell makes and a release without one makes a click again ('#N' replies, links open). Anywhere else a
        press clicks at once, as it always did."""
        event = self.mouse_events.popleft() if self.mouse_events else None
        if event is None:
            return
        _, x, y, _, buttons = event
        if buttons & curses.BUTTON1_CLICKED:
            self.selection = self.press = None
            self.clicked(y, x)
        elif buttons & curses.BUTTON1_PRESSED:
            self.selection, spot = None, self.spot(y, x)
            self.press = (y, x, spot)
            if not spot:
                self.clicked(y, x)
        elif buttons & (curses.REPORT_MOUSE_POSITION | curses.BUTTON1_RELEASED):
            released = buttons & curses.BUTTON1_RELEASED
            if self.press is None:          # curses drops a press read together with what followed it: the
                if released:                # release is the click, the first motion the drag's start
                    self.selection = None
                    self.clicked(y, x)
                    return
                self.selection, self.press = None, (y, x, self.spot(y, x))
            start_y, start_x, anchor = self.press
            end = self.spot(y, x, clamp=True)
            if anchor and end and (y, x) != (start_y, start_x):
                self.selection = (anchor, end)
            if released:
                self.press = None
                if anchor and not self.selection:
                    self.clicked(start_y, start_x)
        elif buttons & curses.BUTTON4_PRESSED:
            self.scroll += WHEEL_ROWS
        elif buttons & curses.BUTTON5_PRESSED:
            self.scroll = max(0, self.scroll - WHEEL_ROWS)
            if not self.scroll:
                self.unseen = {}

    def set_mouse(self, on):
        """Wheel events scroll the chat and a drag selects; while on, the terminal's own selection needs Shift+drag.
        curses asks only for clicks (mode 1000); a drag needs motion with a button held (1002), asked for once
        curses has written its own request (see _show)."""
        self.mouse = on
        self.selection = self.press = None
        clicks = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED | curses.REPORT_MOUSE_POSITION
        curses.mousemask(curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED | clicks if on else 0)
        self.drag_mode = DRAG_ON if on else DRAG_OFF

    def copy_selection(self):
        """Ctrl+C with a selection: its text onto the clipboard, the selection gone so the next Ctrl+C counts
        towards quitting again. False when nothing is selected, or only what the screen adds."""
        text = self.selected_text()
        self.selection = self.press = None
        if not text.strip():
            return False
        lines = text.count("\n") + 1
        said = f"{lines} line{'s' if lines > 1 else ''}"
        if self.writer:                     # GTK's cold start stays off the keyboard
            self.writer.submit("copy", functools.partial(_copy, said), text)
            self.note(f"Copying {said}…")
            return True
        try:
            self.copied(_copy(said, text), None)
        except ValueError as error:
            self.copied(None, error)
        return True

    def copied(self, said, error):
        if error:
            self.note(f"Not copied: {error}", "error")
        else:
            self.note(f"Copied {said}")

    def paste_clipboard(self):
        """Ctrl+V, or an empty paste (a terminal pastes only text): the clipboard's text into the draft, or its
        image attached."""
        if self.busy():
            return
        if self.writer:
            self.writer.submit("clipboard", _read_clipboard)
            self.note("Reading the clipboard…")
            return
        try:
            self.pasted(_read_clipboard(), None)
        except (ValueError, OSError) as error:
            self.pasted(None, error)

    def pasted(self, found, error):
        """What the clipboard held: text goes in at the cursor, as a paste does; an image joins the draft."""
        self.notes = [note for note in self.notes if note[0] != "Reading the clipboard…"]
        if error:
            self.note(f"Not pasted: {error}", "error")
        elif found is None:
            self.note("The clipboard is empty", "error")
        elif found[0] == "text":
            if self.busy():
                self.note("Not pasted: your message is still sending", "error")
                return
            self.paste(found[1].replace("\r\n", "\n").replace("\r", "\n"))
            self.interacted = True
        elif len(self.images) >= store.MAX_ATTACHMENTS:
            self.note(f"A message holds up to {store.MAX_ATTACHMENTS} images", "error")
        else:
            self.images = [*self.images, found[1]]
            self.interacted = True

    def focus(self, gained):
        """The terminal window gained or lost focus. Gaining it counts as the user looking."""
        self.focused = gained
        self.desktop.focused(gained)
        if gained:
            self.interacted, self.away = True, 0

    def paste(self, text):
        """Insert pasted text; a large paste becomes a chip so the composer stays fast. An empty paste (the
        clipboard holds an image, not text) attaches the clipboard's image; a paste that is only image files
        (a file dropped on the terminal pastes its path) attaches them."""
        if self.busy():                     # before any chip bookkeeping
            return
        if not text:
            self.paste_clipboard()
            return
        files = image_files(text)
        if files:
            for path in files:
                self.attach(functools.partial(_attach_file, path), os.path.basename(path))
            return
        lines = text.count("\n") + 1
        if len(text) <= CHIP_CHARS and lines <= CHIP_LINES:   # drawn as typed: tabs as spaces, no control codes
            self.insert("".join(ch for ch in text.replace("\t", "    ")
                                if ch == "\n" or unicodedata.category(ch) != "Cc"))
            return
        self.chip_count += 1
        chip = self._fresh_chip()
        self.chips[chip], self.chip_numbers[chip] = text, self.chip_count
        at = self.snap(self.cursor)             # not insert(): that would treat the chip's own key as text
        self.draft, self.cursor = self.draft[:at] + chip + self.draft[at:], at + 1
        self.menu_index, self.menu_closed, self.history_pos = 0, False, None

    def attach(self, job, name):
        """Store an image off the input thread; apply_writes puts it on the draft (see image_row)."""
        if self.busy():
            return
        if len(self.images) + self.attaching >= store.MAX_ATTACHMENTS:
            self.note(f"A message holds up to {store.MAX_ATTACHMENTS} images", "error")
            return
        self.attaching += 1
        if self.writer:
            self.writer.submit("attach", job)
            return
        try:
            self.attached(job(), None)
        except (ValueError, OSError) as error:
            self.attached(None, error)

    def attached(self, ref, error):
        self.attaching = max(0, self.attaching - 1)
        if error:
            self.note(f"Not attached: {error}", "error")
        elif len(self.images) < store.MAX_ATTACHMENTS:
            self.images = [*self.images, ref]
            self.interacted = True

    def image_row(self, width):
        """The draft's images, above it: "▣ image 1 · 1200×800", with how to drop the last one."""
        if not self.images and not self.attaching:
            return None
        names = [f"▣ image {n} · {ref['width']}×{ref['height']}" for n, ref in enumerate(self.images, 1)]
        if self.attaching:
            names.append("▣ attaching…")
        return fit("   ".join(names) + ("   · Backspace at the start removes the last" if self.images else ""),
                   width - 2)

    def _fresh_chip(self, avoid=""):
        """A private-use character not already in the draft (or `avoid`): chips never collide with text."""
        code = CHIP_FIRST + self.chip_count     # usually the chip's own number, so its label matches after restore
        while chr(code) in self.draft or chr(code) in avoid or chr(code) in self.chips:
            code += 1
        return chr(code)

    def chip_label(self, chip):
        text = self.chips[chip]
        lines = text.count("\n") + 1
        number = self.chip_numbers.get(chip, ord(chip) - CHIP_FIRST)
        return f"[Pasted text #{number} · {lines} lines]" if lines > 1 else f"[Pasted text #{number} · {len(text)} chars]"

    def expand(self, text):
        """Chips back to their pasted text; nothing else in the draft can be mistaken for one."""
        return "".join(self.chips.get(ch, ch) for ch in text) if self.chips else text

    def chip_spans(self):
        return [(index, index + 1) for index, ch in enumerate(self.draft) if ch in self.chips]

    def snap(self, position, forward=True):
        """Move a position out of the inside of a chip or a visible character (👍🏽, é), so both edit whole."""
        for start, end in self.chip_spans():
            if start < position < end:
                return end if forward else start
        bounds = clusters(self.draft, self.chips)
        if position in bounds:
            return position
        if forward:
            return next((b for b in bounds if b > position), len(self.draft))
        return max((b for b in bounds if b < position), default=0)

    def _step(self, position, forward):
        """The next visible-character boundary after (or before) `position`."""
        bounds = clusters(self.draft, self.chips)
        if forward:
            return next((b for b in bounds if b > position), len(self.draft))
        return max((b for b in bounds if b < position), default=0)

    def cut(self, start, end):
        """Delete draft[start:end], widened to whole chips; forget chips no longer in the draft."""
        self.history_pos = None             # an edited recalled entry is the draft now: Up mustn't discard it
        if self.busy():
            return
        for chip_start, chip_end in self.chip_spans():
            if chip_start < end and start < chip_end:
                start, end = min(start, chip_start), max(end, chip_end)
        self.draft = self.draft[:start] + self.draft[end:]
        self.cursor = self.snap(start, forward=False)   # what's left may have joined into one character
        self.chips = {chip: text for chip, text in self.chips.items() if chip in self.draft}
        self.menu_index, self.menu_closed = 0, False

    def busy(self):
        """True while a send is in flight (the rule above the box says so): the draft being sent can't change."""
        return bool(self.sending)

    def insert(self, text):
        if self.busy():
            return
        self.menu_index, self.menu_closed = 0, False
        self.cursor = self.snap(self.cursor)
        for chip in [chip for chip in self.chips if chip in text]:   # literal text keeps its characters
            fresh = self._fresh_chip(avoid=text)
            self.draft = self.draft.replace(chip, fresh)
            self.chips[fresh] = self.chips.pop(chip)
            self.chip_numbers[fresh] = self.chip_numbers.pop(chip, ord(chip) - CHIP_FIRST)
        self.draft = self.draft[:self.cursor] + text + self.draft[self.cursor:]
        self.cursor = self.snap(self.cursor + len(text))   # "e" typed before a lone accent makes é: past it
        self.history_pos = None

    def newline(self):
        """A typed new line (never a paste): continues a list, so "4. x" is followed by "5. ", and an empty
        item after other items ends the list instead. A lone "1." starts one."""
        if self.busy():                     # a send is in flight: the draft is frozen until it lands
            return
        if self._answer_newline():
            return
        found = LIST_ITEM.match(self.draft, self._line_bounds()[0])
        if not found or self.cursor < found.end():
            self.insert("\n")
            return
        start, end = self._line_bounds()
        above = self.draft.rfind("\n", 0, start - 1) + 1 if start else None
        listed = above is not None and LIST_ITEM.match(self.draft[above:start - 1])
        if self.cursor == end and not self.draft[found.end():end].strip() and listed:   # an empty item ends it
            self.cut(found.start(), end)
            return
        indent, number, style, bullet = found.groups()
        self.insert("\n" + indent + (f"{int(number) + 1}{style}" if number else bullet) + " ")

    def _answer_newline(self):
        """Answers to numbered questions continue like a list: at the end of "1a", a new line starts with "2", for
        the letter. Only a 1 starts a run and each later line must follow the one above, so a
        line that merely starts with a number and a letter is left alone; a new line on a lone number ends it."""
        start, end = self._line_bounds()
        if self.cursor != end:
            return False
        run = answer_run(self.draft[:start].split("\n")[:-1])
        follows = lambda number: run and run + 1 == number
        line = self.draft[start:end]
        found = ANSWER.match(line)
        if found and (found.group(2) == "1" or follows(int(found.group(2)))):
            self.insert("\n" + found.group(1) + str(int(found.group(2)) + 1))
            return True
        lone = re.fullmatch(r"[ \t]*(\d{1,2})", line)
        if lone and follows(int(lone.group(1))):
            self.cut(start, end)            # the run ends: the empty line stays, as an emptied list item does
            return True
        return False

    def word_left(self):
        while self.cursor and self.draft[self.cursor - 1].isspace():
            self.cursor -= 1
        while self.cursor and not self.draft[self.cursor - 1].isspace():
            self.cursor -= 1

    def word_right(self):
        size = len(self.draft)
        while self.cursor < size and self.draft[self.cursor].isspace():
            self.cursor += 1
        while self.cursor < size and not self.draft[self.cursor].isspace():
            self.cursor += 1

    def delete_word(self):
        end = self.cursor
        self.word_left()
        self.cut(self.cursor, end)

    def menu(self):
        """Find command names, related actions, or choices for a command's argument."""
        if (self.menu_closed or not self.draft.startswith("/") or self.draft.startswith("//")
                or "\n" in self.draft or "\r" in self.draft):
            return []
        command, *rest = self.draft.split(maxsplit=1)
        query = command.casefold()
        if self.draft != command:
            return self._argument_menu(query, rest[0].strip() if rest else "")
        names = [name for name in COMMANDS if name.startswith(query)]
        if not names:
            return [MenuChoice(name, f"{name} {hint}".rstrip(), description, False)
                    for name, (hint, description) in COMMAND_HELP.items()
                    if query[1:] in description.casefold()]
        choices = []
        for name in names:
            hint, description = COMMAND_HELP[name]
            # its whole name typed, and its arguments optional ("[…]"): Enter runs it, as /export always did; the newcomer
            # run had to press Enter twice to open /todo (#6130)
            default = hint.startswith("[") and name == query
            repeat_find = name == "/find" and self.found
            choices.append(MenuChoice(name, f"{name} {hint}".rstrip(), description,
                                      not hint or default or bool(repeat_find)))
        return choices

    def _argument_menu(self, command, partial):
        if command == "/release":
            available = [(path, "release your claim") for path, owner in sorted(self.claims.items())
                         if owner == store.USER and not path.startswith("task:")]
        else:
            available = ARGUMENT_CHOICES.get(command, ())
            partial = partial.casefold()
        choices = []
        for argument, description in available:
            if argument.startswith(partial):
                draft = f"{command} {argument}"
                choices.append(MenuChoice(draft, clean(draft), description, partial == argument))
        return choices

    def menu_pick(self, run):
        """Fill in the selected command; with run=True, also run it when it takes no arguments."""
        if self.busy():
            return True
        options = self.menu()
        choice = options[min(self.menu_index, len(options) - 1)]
        self.history_pos = None
        if run and choice.runnable:
            self.draft, self.cursor = choice.draft, len(choice.draft)
            return self.submit(choice.draft)
        argument = " " in choice.draft
        self.draft = choice.draft if argument else choice.draft + " "
        self.cursor, self.menu_index, self.menu_closed = len(self.draft), 0, argument
        if choice.draft == "/reply" and not run:
            self.pick_reply()
        return True

    def pick_reply(self):
        """Fill in /reply's number: the newest message that mentions the user or answers them, and on each
        further Tab the one before it."""
        targets = [message for message in reversed(self.messages)
                   if message["kind"] == "msg" and message["sender"] != store.USER and self.addressed_to_me(message)]
        if not targets:
            self.note("Nothing mentions or answers you yet")
            return
        shown = REPLY_DRAFT.fullmatch(self.draft)
        current = int(shown.group(1)) if shown and shown.group(1) else None
        numbers = [message["id"] for message in targets]
        target = targets[(numbers.index(current) + 1) % len(targets)] if current in numbers else targets[0]
        self.draft = f"/reply #{target['id']}\n"     # the answer on the line below (#3272)
        self.cursor = len(self.draft)
        self.note(f"#{target['id']} {target['sender']}: {' '.join(REPLY.sub('', clean(target['text'])).split())}", "quote", sticky=True)

    def open_link(self, target):
        """A clicked link opens in the desktop's own app for it (a browser, a Markdown viewer, Files)."""
        from agi.ui.render import safe_target
        path, refused = safe_target(target)
        if refused:
            self.note(f"Not opened: {refused}", "error")
            return
        try:
            opener = os.environ.get("AGI_OPEN", "xdg-open")   # tests stand in a recorder here
            subprocess.Popen([opener, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            self.note(f"Not opened: {error.strerror or error}", "error")
            return
        site = urllib.parse.urlsplit(path).hostname if re.match(r"https?://", path) else None
        self.note(f"Opening {site or os.path.basename(path.rstrip('/')) or path}")   # a page: its site, not "4"

    def start_reply(self, message_id):
        """The reply button (a click on '#N'): '/reply #N' on a line of its own, the answer below it, in front of
        whatever is already typed."""
        if self.busy():
            return
        typed = REPLY_DRAFT.sub("", self.draft, count=1) if self.draft.startswith("/reply") else self.draft
        self.draft = f"/reply #{message_id}\n" + typed.lstrip()
        self.cursor, self.history_pos = len(self.draft), None
        target = self.by_id.get(message_id)
        if target:
            self.note(f"#{message_id} {target['sender']}: {' '.join(REPLY.sub('', clean(target['text'])).split())}", "quote", sticky=True)

    def set_highlight(self, words):
        if words != self.highlight:
            self.highlight = words
            self.cache.clear()              # rows were drawn with the old highlight

    def enter_rail(self):
        """/tasks beside the rail: the keyboard moves into its tasks, newest first."""
        self.show_board()                   # the whole list below as before, with what the rail cuts (reminders, results)
        tasks = self.rail_tasks()
        if tasks:
            self.rail_focus = tasks[0][0]["id"]
            self.notes = self.notes + [("↑ ↓ choose a task in the rail · Enter opens it · Esc back to typing", "dim",
                                        UNTIL_KEY)]

    def leave_rail(self):
        self.rail_focus = None
        self.notes = []

    # --- joining, leaving and claims (#5982, #6005, #6007) ---------------------------------------------------
    def ask(self, question, yes, no=None, keep=False, *, keep_draft=False):
        """Enter accepts on an empty box, or with a draft when the action keeps it. Esc declines."""
        self.confirming = (yes, no, keep_draft)
        row = (f"Enter yes · Esc no · {question}", "user", None)   # keys first: a long question gets cut at its
                                                                     # end, and the newcomer run lost them (#6130)
        self.notes = self.notes + [row] if keep else [row]

    def answer(self, yes):
        action = self.confirming[0 if yes else 1]
        self.confirming = None
        self.notes = []
        if action:
            action()

    def linked_rows(self):
        """The rows of the shown list that open something: (index, kind, number) for each one's first link."""
        from agi.ui.render import Renderer
        rows = []
        for index, (text, _, _) in enumerate(self.notes):
            if index < self.pick_from:
                continue
            if index in self.row_actions:
                rows.append((index, "action", index))
                continue
            spots = Renderer.note_links(text, 0)
            if spots:
                rows.append((index, spots[0][2], spots[0][3]))
        return rows

    def here(self):
        return (self.view, self.row_owners[0] if self.scroll and getattr(self, "row_owners", None) else None)

    def keep_place(self):
        """Where you were, for Tab to return to after a list's hits: as it was when you typed the command's /,
        before its menu took a row of the chat (#6116), else now; saved before /find jumps (#6096)."""
        here, self.command_place = self.command_place or self.here(), None
        if not self.jumped_from or self.jumped_from[-1] != here:
            self.jumped_from = (self.jumped_from + [here])[-20:]

    def start_pick(self, first=0):
        """A list was just shown from row `first`: its first row with a link is chosen, so ↓ and Enter work at once
        (#6083)."""
        self.pick_from = first
        rows = self.linked_rows()
        self.pick = rows[0][0] if rows else None

    def pick_key(self, key):
        """↑↓ choose a row of the list, Enter opens it as a click would (Tab then returns); False for any other key,
        which closes the list."""
        rows = self.linked_rows()
        indexes = [index for index, _, _ in rows]
        if self.draft or self.pick not in indexes:
            return False
        if key in (curses.KEY_UP, curses.KEY_DOWN):
            here = indexes.index(self.pick)
            self.pick = indexes[max(0, min(len(indexes) - 1, here + (1 if key == curses.KEY_DOWN else -1)))]
            return True
        if key in ("\r", curses.KEY_ENTER):
            _, kind, number = rows[indexes.index(self.pick)]
            if kind == "action":
                self.row_actions[number]()
            elif kind == "todo":
                self.open_todo(number)
            else:                           # one Tab back to where the list was opened, however many hits you
                back = list(self.jumped_from)   # look at (#6096)
                self.go_to(number)
                self.jumped_from = back
            return True
        return False

    # --- /update and /backup (#6039, #6041): slow work off the keyboard, its result taken by the main loop --------
    # --- /settings (#6043): one list, ↑↓ choose, Enter changes it at once; the commands stay ------------------------
    def show_settings(self, chosen=None):
        try:
            saved = ui_state.user_settings()
        except (OSError, ValueError):
            saved = {"notifications": self.notify_on, "mouse": self.mouse}
        policy = (self.guard or {}).get("mode") or "normal"
        rows = [("Settings · ↑↓ choose · Enter changes · Esc closes", "key", None),
                (f"Sound when you're mentioned: {sound.mode()}", "dim", self.cycle_sound),
                (f"Desktop notifications: {'on' if saved['notifications'] else 'off'}", "dim",
                 lambda: self.toggle_setting("notifications")),
                (f"Mouse scrolls the chat: {'on' if saved['mouse'] else 'off (select text normally)'}", "dim",
                 lambda: self.toggle_setting("mouse")),
                (f"Agents' posting: {policy}", "dim", self.cycle_policy),
                (f"Chat folder: {str(store.DATA_DIR).replace(os.path.expanduser('~'), '~', 1)}", "dim", None)]
        self.notes = [(text, kind, UNTIL_KEY) for text, kind, _ in rows]
        self.row_actions = {index: action for index, (_, _, action) in enumerate(rows) if action}
        self.start_pick(1)
        if chosen is not None:
            self.pick = chosen

    def settings_changed(self):
        self.show_settings(self.pick)

    def cycle_sound(self):
        modes = sound.MODES
        sound.set_mode(modes[(modes.index(sound.mode()) + 1) % len(modes)])
        self.settings_changed()

    def toggle_setting(self, name):
        try:
            now = ui_state.set_user_setting(name, not ui_state.user_settings()[name])
        except (OSError, ValueError) as error:
            self.note(f"Not changed: {error}", "error")
            return
        if name == "mouse":
            self.set_mouse(now["mouse"])
        else:
            self.notify_on = now["notifications"]
        self.settings_changed()

    def cycle_policy(self):
        policy = (self.guard or {}).get("mode") or "normal"
        command = {"normal": ("/mode", "wait"), "wait": ("/pause", ""), "paused": ("/resume", "")}.get(policy,
                                                                                                    ("/mode", "normal"))
        if self.set_mode(*command):
            self.settings_changed()

    def in_background(self, work, then):
        def run():
            try:
                result = work()
            except Exception as error:      # shown by `then`, never a dead thread
                result = error
            self.background = (then, result)
        threading.Thread(target=run, daemon=True).start()

    def take_background(self):
        """The main loop's look for a finished /update or /backup: True if there was one (redraw)."""
        if self.background is None:
            return False
        then, result = self.background
        self.background = None
        then(result)
        return True

    def update_check(self):
        self.note("Checking for a newer AGI…", sticky=True)
        self.in_background(installation.check_update, self.update_found)

    def update_found(self, offer):
        if isinstance(offer, Exception):
            self.note(f"Couldn't check for an update: {offer}", "error", sticky=True)
            return
        if offer is None:
            self.note(f"AGI {__version__} is the newest release.")
            return
        lines = [line for line in installation.update_notes(offer).splitlines() if line.strip()][:12]
        self.notes = ([(f"AGI {offer.version} is out · what's new:", "key", UNTIL_KEY)]
                      + [("  " + line, "dim", UNTIL_KEY) for line in lines])
        self.ask(f"Install AGI {offer.version}? Your chat stays as it is", lambda: self.update_apply(offer), keep=True)

    def update_apply(self, offer):
        self.note(f"Installing AGI {offer.version}: download, check, switch…", sticky=True)
        self.in_background(lambda: installation.apply_update(offer), lambda result: self.update_done(offer, result))

    def update_done(self, offer, launcher):
        if isinstance(launcher, Exception):   # the old version is still the one that runs (Codex's install tests)
            self.note(f"Not installed: {launcher} · AGI {__version__} stays as it was", "error", sticky=True)
            return
        self.ask(f"AGI {offer.version} is installed. Restart now? Your draft and your place stay",
                 lambda: self.restart_into(launcher), keep_draft=True)

    def restart_into(self, launcher):
        """Close as /quit does (open writes finish first, #6041), then start the new version in this terminal."""
        self.restart_with = str(launcher)
        self.running = False

    def backup_saved(self, path):
        if isinstance(path, Exception):
            self.note(f"Backup not saved: {path}", "error", sticky=True)
            return
        folder = os.path.dirname(str(path)).replace(os.path.expanduser("~"), "~", 1)
        self.note(f"Backup saved · {os.path.basename(str(path))} · in {folder}", sticky=True)

    def start_todo_add(self):
        """The box gets /todo add, and Enter goes through its yes/no. A message already being typed is kept: ↑
        brings it back, as after Esc clears a draft (#6096)."""
        if self.todo_focus is not None:
            self.leave_todo()
        kept = self.draft.strip() and not self.draft.startswith("/todo add")
        if kept:                            # back in the box once the to-do is saved or cancelled (#6103)
            self.kept_draft = (self.draft, dict(self.chips))
            self.chips = {}
        self.draft, self.cursor = "/todo add ", len("/todo add ")
        if kept:
            self.note("Your message is kept · it comes back after the to-do")

    def add_own_todo(self, text):
        """/todo add check the release tomorrow 9:00 (#6054): a to-do of your own, with the time it read shown for a
        yes/no before anything is saved; a time it can't read is refused with examples, never guessed."""
        if not text:
            self.note("Usage: /todo add what [when] · e.g. /todo add check the release tomorrow 9:00", "error")
            return False
        try:
            title, due = when.parse(text)
        except ValueError as error:
            self.note(f"{error} · e.g. 30m · 2h · tomorrow 9:00 · fri 17:00 · 2026-11-25", "error")
            return False

        def save():
            try:
                todo = store.add_personal_todo(title, due_at=due.timestamp() if due else None)
            except (OSError, ValueError) as error:
                self.note(f"To-do not added: {error}", "error")
                return
            self.note(f"Added T{todo['id']} for you" + (f" · due {when.say(due)}" if due else "") + " · /todo lists it")
            self.request_status()

        def then(action):
            def run():
                if action:
                    action()
                self.restore_kept_draft()
            return run

        self.ask("Add a to-do for you?", then(save), then(None))
        schedule = f"due {when.say(due)}" if due else "no due time"
        self.notes.insert(0, (f"{schedule} · {title}", "user", None))
        return True

    def restore_kept_draft(self):
        """The message you were typing before Ctrl+A, back in the box, unless you've typed something else since."""
        if self.kept_draft and not self.draft.strip():
            (self.draft, self.chips), self.kept_draft = self.kept_draft, None
            self.cursor = len(self.draft)

    def membership_command(self, command, agent):
        if agent not in store.AGENTS:
            self.note(f"Usage: {command} claude · {command} codex", "error")
            return False
        name = agent.capitalize()
        try:
            state = membership.status(agent)
        except ValueError as error:
            self.note(f"{name}: {error}", "error")
            return False
        resume = membership.resume if agent == "claude" else codex_resume
        if command == "/leave":
            self.membership_step(membership.leave, agent,
                                 f"{name} left · its claims stay · /join {agent} brings it back")
            return True
        pending = state.get("pending") if state.get("pending_reachable") else None
        if pending:
            self.ask(f"Another {name} session asked to join: move {name} there? The current one stops getting messages",
                     lambda: self.membership_step(functools.partial(resume, session=pending), agent,
                                                  f"{name} moved to the new session"),
                     lambda: self.membership_step(functools.partial(membership.decline, session=pending), agent,
                                                  f"{name} stays in its session"))
            return True
        if not state.get("session") or (agent == "claude" and not state.get("reachable")):
            # Codex's watcher may be stopped while its session is fine: bridge.resume checks and starts it (#6026)
            self.note(f"No {name} session is connected · {START_LINE[agent]}", "error", sticky=True)
            return False
        return self.membership_step(resume, agent, f"{name} joined · messages reach it again")

    def membership_step(self, step, agent, done):
        try:
            step(agent)
        except (ValueError, OSError) as error:
            self.note(f"{agent.capitalize()}: {error}", "error")
            return False
        self.note(done)
        self.request_status()
        return True

    def unclaim(self, argument):
        number = argument.lstrip("#")
        if not number.isdigit():
            self.note("Usage: /unclaim #N · takes an agent's claim off request N", "error")
            return False
        try:
            claim = store.task_claim(int(number))
        except ValueError as error:
            self.note(str(error), "error")
            return False
        if not claim:
            self.note(f"Nobody holds #{number}", "error")
            return False
        doing = f" (doing: {claim['doing']})" if claim.get("doing") else ""
        self.ask(f"Take #{number} from {claim['owner']}{doing}? It goes back to open",
                 lambda: self.release_claim(int(number), claim))
        return True

    def release_claim(self, number, claim):
        try:
            store.release_task_claim(number, claim["claim_id"])
        except (ValueError, OSError, store.ClaimConflict) as error:
            self.note(f"#{number} not released: {error}", "error")
            return
        self.note(f"#{number} released from {claim['owner']}")
        self.request_status()

    def escape(self):
        now = time.time()
        if self.confirming:                 # a question waiting for Enter: Esc says no
            self.answer(False)
            return
        notes = getattr(self, "notes", ())
        if notes and notes[0][2] is UNTIL_KEY and not self.draft:
            self.interacted = False         # closing a list (/catchup, /find, /who…) isn't reading the chat (#6113)
        if self.selection:                  # first, the selection goes, as a click elsewhere would
            self.selection = self.press = None
            return
        if self.todo_open is not None:      # /todo: from one to-do back to the list, then out of the list
            self.todo_open, self.todo_scroll = None, 0
            return
        if self.todo_focus is not None:
            self.leave_todo()
            return
        if self.rail_focus is not None:     # first, out of the rail, back to the box
            self.leave_rail()
            return
        self.set_highlight(None)
        self.rail_open = None               # a task opened in the rail closes with the rest
        if self.help:
            self.help = False
            if self.asked and self.draft == "?":   # only looking at the keys: the ? goes with them
                self.cut(0, 1)
            self.asked = False
        elif self.menu():
            self.menu_closed = True         # hide the list, keep the text
        elif self.view is not None or self.scroll:
            self.jump_to_latest()
        elif (self.draft or self.images) and now - self.esc_at < QUIT_WINDOW and not self.busy():
            self.stash, self.stash_chips = self.draft, dict(self.chips)   # the text and its chips go together,
            self.stash_images, self.images = self.images, []              # and its images
            self.draft, self.cursor, self.chips = "", 0, {}
            self.note("Draft cleared · ↑ brings it back")
            self.esc_at = 0
            return
        if getattr(self, "whatsnew", None) and not self.notes:   # nothing else left to close: the new-version line
            self.acknowledge_release()
        self.notes = []
        self.esc_at = now

    def jump_to_latest(self):
        """Back to the newest messages, out of history or a scroll: Esc's job once nothing else is open, and the
        centred jump button's whatever is open (Codex, #4373: it closed the / menu instead)."""
        self.view, self.scroll, self.unseen = None, 0, {}

    def _line_bounds(self):
        start = self.draft.rfind("\n", 0, self.cursor) + 1
        end = self.draft.find("\n", self.cursor)
        return start, len(self.draft) if end < 0 else end

    def vertical(self, step):
        """Up/Down move by the rows on screen, so a long wrapped line isn't skipped; past the top or bottom row
        they jump to the start or end of the draft. An empty draft (and entries recalled from one) walk the sent
        history instead, so recalling stays one key; with text in the box the history is Shift+Up/Down."""
        width = self.chat_width()
        rows, starts, (row, column) = self._composer_layout(width)
        target = row + step
        if not 0 <= target < len(rows):
            if not self.draft or (self.history_pos is not None and not self.saved_draft[0]):
                self.recall(step)           # one-key recall: from an empty box ↑↑ keeps walking back, ↓ returns
            else:                           # a terminal can't report a held key: the edge row is the "hold"
                self.cursor = 0 if step < 0 else len(self.draft)
            return
        offset, used = starts[target], 0
        for ch in rows[target]:
            size = self._cells(ch, width)
            if used + size > column:
                break
            used += size
            offset += 1
        wrapped = target + 1 < len(starts) and starts[target + 1] == starts[target] + len(rows[target])
        if wrapped and offset == starts[target + 1] and rows[target]:
            offset -= 1                     # wrapping puts the row boundary on the following line; a
                                            # terminal 1 or 2 columns wide has empty rows (crashed: playtest, #1941)
        self.cursor = self.snap(offset, forward=step > 0)

    def recall(self, step):
        if self.busy():
            return
        if not self.history and not self.stash:
            return
        # each entry is text with its own chips: sent text is already expanded, a stash keeps its pastes
        entries = [(text, {}, []) for text in self.history]
        if (self.stash or self.stash_images) and self.stash not in self.history[-1:]:
            entries.append((self.stash, self.stash_chips, self.stash_images))
        if self.history_pos is None:
            if step > 0:
                return
            self.saved_draft = (self.draft, dict(self.chips), list(self.images))
            self.history_pos = len(entries)
        self.history_pos += step
        if self.history_pos >= len(entries):
            self.history_pos = None
            self.draft, chips, images = self.saved_draft
        else:
            self.history_pos = max(0, self.history_pos)
            self.draft, chips, images = entries[self.history_pos]
        self.chips, self.images = dict(chips), list(images)
        self.cursor = len(self.draft)

    def complete(self):
        """Tab: fill in the @name, /command or /reply number at the cursor. False when there's nothing to fill in,
        so Tab can go back after a jump instead, typed text or not."""
        if self.busy():
            return True
        if self.cursor == len(self.draft) and REPLY_DRAFT.fullmatch(self.draft):
            self.pick_reply()
            return True
        start = self.cursor
        while start and not self.draft[start - 1].isspace():
            start -= 1
        token = self.draft[start:self.cursor]
        if token.startswith("@"):
            pool = NAMES
        elif token.startswith("/") and start == 0:
            pool = COMMANDS
        else:
            return False
        matches = [option for option in pool if option.startswith(token.lower())]
        if len(matches) == 1:
            self.draft = self.draft[:start] + matches[0] + " " + self.draft[self.cursor:]
            self.cursor = start + len(matches[0]) + 1
            self.history_pos = None
        elif matches:
            common = os.path.commonprefix(matches)
            self.draft = self.draft[:start] + common + self.draft[self.cursor:]
            self.cursor = start + len(common)
            self.history_pos = None
            self.note("  ".join(matches))
        return True

    def key(self, key):
        """Handle one key. Returns False to quit."""
        if self.sending and isinstance(key, str) and key != "\x03":
            self.typed_ahead.append(key)    # text typed while a send lands: kept in order and applied after
            return True                     # it, never dropped (a slow USB fsync can hold a send for seconds);
                                            # scrolling and other named keys still act at once
        height = self.win.getmaxyx()[0]
        if isinstance(key, int) and key > 255:
            try:
                name = curses.keyname(key)
            except curses.error:            # no curses screen (unit tests)
                name = b""
            if name in (b"kxIN", b"kxOUT"):
                self.focus(name == b"kxIN")     # terminfo-decoded focus report
                return True
            if name in HISTORY_KEYS:
                self.interacted = True
                self.recall(HISTORY_KEYS[name])
                return True
            word = {b"kLFT3": self.word_left, b"kLFT5": self.word_left, b"kRIT3": self.word_right,
                    b"kRIT5": self.word_right}.get(name)
            if word:                        # Alt/Ctrl+Left/Right, as curses decodes them: a word at a time
                self.interacted = True
                word()
                return True
        if key == "/" and not self.draft:   # a command starts: remember the reading place before its menu opens
            self.command_place = self.here()
        if self.pick is not None and key not in (curses.KEY_MOUSE, curses.KEY_RESIZE) and self.pick_key(key):
            return True                     # a list's keys aren't reading: nothing below is marked read (#6096)
        if key == "\x01" and getattr(self, "board_kind", None) == "todo" and self.notes and self.notes == self.board:
            self.start_todo_add()           # Ctrl+A with /todo shown, even an empty one (#6096)
            return True
        if key != curses.KEY_RESIZE:        # a resize isn't the user reading: it mustn't move the read marker
            self.interacted = True
        if key == curses.KEY_MOUSE:          # wheel, click or drag: the transcript, never the sent history
            self.pointer()
            return True
        if self.todo_focus is not None and self.todo_key(key):
            return True
        if self.rail_focus is not None:     # in the rail: arrows choose a task, Enter opens it; typing goes back
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                ids = [task["id"] for task, _, _ in self.rail_tasks()]
                here = ids.index(self.rail_focus) if self.rail_focus in ids else 0
                self.rail_focus = ids[(here + (1 if key == curses.KEY_DOWN else -1)) % len(ids)] if ids else None
                return True
            if key in ("\r", curses.KEY_ENTER):
                self.rail_clicked(("task", self.rail_focus))
                return True
            self.leave_rail()
        self.pick = None
        self.row_actions = {}
        if key not in ("\x03",) and self.notes and self.notes[0][2] is not None:
            self.notes = []
        if key == "\x03":                   # Ctrl+C
            if self.closing:                # already waiting for the disk: this one forces the quit
                self.force_quit = True
                return False
            if self.selection and self.copy_selection():
                return True
            if time.time() - self.ctrl_c_at < QUIT_WINDOW:
                return False
            self.ctrl_c_at = time.time()
            return True
        if key == "?" and not self.draft and not self.busy():   # the keys, and a ? that stays as you type
            self.insert("?")                                     # on ("?why" works), so a message can start
            self.help = self.asked = True                        # with one
            return True
        if self.asked and self.draft == "?" and key in ("\r", curses.KEY_ENTER):
            self.cut(0, 1)                  # Enter after ? closes the keys; it doesn't send a lone "?"
            self.help = self.asked = False
            return True
        self.help = self.asked = False
        options = self.menu()
        if options and key in (curses.KEY_UP, curses.KEY_DOWN):
            step = 1 if key == curses.KEY_DOWN else -1
            self.menu_index = (min(self.menu_index, len(options) - 1) + step) % len(options)
            return True
        if options and key in ("\t", "\r", curses.KEY_ENTER):
            self.menu_pick(run=key != "\t")
            return self.running
        if (key in ("\r", curses.KEY_ENTER) and self.confirming
                and (not self.draft.strip() or self.confirming[2])):
            self.answer(True)
            return self.running             # a yes that closes the screen (/update's restart) must end the loop
        if self.confirming and key != curses.KEY_RESIZE:   # typing instead of answering: the question goes, unanswered,
            self.confirming, self.notes = None, []          # so a later Enter can't say yes to it
        if key in ("\r", curses.KEY_ENTER):
            if self.busy():
                return True
            if shift_held():                # Shift+Enter: a new line (the terminal sends it as plain Enter)
                self.newline()
                return True
            text = self.draft.strip("\n").rstrip()  # keep leading spaces: pasted code stays indented
            if text.strip() or self.images:         # images alone are a message too
                self.submit(text)
            return self.running
        if key == "\n":                     # Ctrl+J
            self.newline()
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if self.cursor:                 # a chip, or a whole 👍🏽, goes in one press
                self.cut(self._step(self.cursor, False), self.cursor)
            elif self.images and not self.busy():   # at the start: the last image goes
                self.images = list(self.images[:-1])
        elif key == "\x16":                # Ctrl+V: the clipboard's text or image (the terminal's own paste
            self.paste_clipboard()         # key sends text as a bracketed paste)
        elif key == curses.KEY_DC or key == "\x04":
            if self.cursor < len(self.draft):
                self.cut(self.cursor, self._step(self.cursor, True))
        elif key == curses.KEY_LEFT:
            self.cursor = self._step(self.cursor, False)
        elif key == curses.KEY_RIGHT:
            self.cursor = self._step(self.cursor, True)
        elif key in (curses.KEY_HOME, "\x01"):
            self.cursor = self._line_bounds()[0]
        elif key in (curses.KEY_END, "\x05"):
            self.cursor = self._line_bounds()[1]
        elif key == curses.KEY_UP:
            self.vertical(-1)
        elif key == curses.KEY_DOWN:
            self.vertical(1)
        elif key in HISTORY_KEYS:           # Shift/Alt/Ctrl+Up/Down: the sent history, whatever is in the box
            self.recall(HISTORY_KEYS[key])
        elif key == "\x15":                 # Ctrl+U: delete to line start
            self.cut(self._line_bounds()[0], self.cursor)
        elif key == "\x0b":                 # Ctrl+K: delete to line end
            cursor = self.cursor
            self.cut(cursor, self._line_bounds()[1])
            self.cursor = min(cursor, self.cursor)
        elif key == "\x17":                 # Ctrl+W
            self.delete_word()
        elif key == "\t":
            if not self.complete() and self.jumped_from:
                self.go_back()
        elif key == curses.KEY_PPAGE:
            self.scroll += max(1, (height - 8) // 2)
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - max(1, (height - 8) // 2))
            if not self.scroll:
                self.unseen = {}
        elif key == "\x0c":                 # Ctrl+L: a full repaint, inside the next frame
            self.win.clearok(True)
        elif key == curses.KEY_RESIZE:     # the app's resize handler already cleared: once is enough
            curses.update_lines_cols()
        elif isinstance(key, str) and (key.isprintable() or key == " "):
            self.insert(key)
        return True

    def set_mode(self, command, argument):
        """/custom N M [D], /custom off, /mode normal|wait, /pause, /resume. False (with a note) if unusable."""
        if not hasattr(store, "set_policy"):
            self.note("Modes need the store update Codex is building", "error")
            return False
        parts = argument.split()
        if command == "/custom" and parts == ["off"] or command == "/mode" and parts == ["normal"]:
            self.set_guard(store.set_policy("normal"))
        elif command == "/mode" and parts == ["wait"]:
            self.set_guard(store.set_policy("wait"))
        elif command == "/pause" and not parts:
            self.set_guard(store.set_policy("paused"))
        elif command == "/resume" and not parts:
            self.set_guard(store.resume_policy())
        elif command == "/custom" and len(parts) in (2, 3) and all(part.isdigit() and int(part) > 0 for part in parts):
            limit, minutes, *lasting = (int(part) for part in parts)       # no D: until you change it (#1823)
            self.set_guard(store.set_policy("custom", limit=limit, window_s=minutes * 60,
                                            duration_s=lasting[0] * 60 if lasting else None))
        else:
            usage = {"/custom": "/custom 10 1 60 (10 messages per 1 min, for 60 min) · /custom 10 1 (no end) · /custom off",
                     "/mode": "/mode normal · /mode wait", "/pause": "/pause", "/resume": "/resume"}[command]
            self.note(f"Usage: {usage}", "error")
            return False
        return True

    @staticmethod
    def exports_folder():
        """Where /export writes: AGI_EXPORTS, else `exports` beside the chat's data folder, so a test or a
        second chat never writes into another one's (and no path is built in)."""
        return os.environ.get("AGI_EXPORTS") or os.path.join(os.path.dirname(os.path.abspath(store.DATA_DIR)),
                                                                   "exports")

    def export(self, argument):
        """Write the whole history, or ids 'A-B', to a Markdown file. Returns the path, or None after a note."""
        first, last = 1, None
        if argument:
            bounds = re.fullmatch(r"#?(\d{1,12})\s*-\s*#?(\d{1,12})", argument)
            if not bounds or int(bounds.group(1)) > int(bounds.group(2)):
                self.note("Usage: /export (everything) · /export 300-404", "error")
                return None
            first, last = int(bounds.group(1)), int(bounds.group(2))
        messages = [m for m in store.read_messages(first - 1) if last is None or m["id"] <= last]
        if not messages:
            self.note("Nothing to export in that range", "error")
            return None
        lines = [f"# AGI #{messages[0]['id']}–#{messages[-1]['id']}", ""]
        for message in messages:
            name = "you" if message["sender"] == store.USER else message["sender"]
            when = message["ts"][:16].replace("T", " ")
            if message["kind"] == "msg":
                lines += [f"**#{message['id']} {name}** · {when}", "", message["text"], ""]
            else:
                lines += [f"*#{message['id']} {name} {message['kind']}: {message['text']}* · {when}", ""]
        folder = self.exports_folder()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{datetime.now():%Y-%m-%d-%H%M%S}-chat-{messages[0]['id']}-{messages[-1]['id']}.md")
        with open(path, "w", encoding="utf-8") as out:
            out.write("\n".join(lines))
        return path

    def who(self):
        """A row per agent: state, unread messages, when it last spoke, and how fast it handles them."""
        rows = []
        for agent in store.AGENTS:
            state = self.presence.get(agent, {}).get("state", "unknown")
            acked = store.get_ack(agent)
            unread = sum(1 for m in self.messages if m["id"] > acked and m["kind"] == "msg" and m["sender"] != agent)
            spoke = next((m for m in reversed(self.messages) if m["sender"] == agent and m["kind"] == "msg"), None)
            ago = "not yet" if spoke is None else self._ago(spoke)
            timing, kind = self._timing(agent)
            rows.append((f"{agent}: {state} · {unread} unread · spoke {ago}{timing}", kind))
            connection = self.connection_row(agent)
            if connection:
                rows.append(connection)
        return rows

    def connection_row(self, agent):
        """Under an agent's /who row (#6020, #6107): left by choice, a connection that stopped (when it was last in
        touch and the one fix), or, for Claude, whether a delivery test has proven messages reach it."""
        if agent == "codex":
            return self.codex_row()
        member = self.presence.get(agent, {}).get("member") or {}
        if member.get("session") and not member.get("joined"):
            return (f"  left the chat · /join {agent} brings it back", "dim")
        if member.get("joined") and not member.get("reachable"):
            contact = member.get("last_contact")
            since = f" · last contact {duration(time.time() - contact)} ago" if contact else ""
            fix = ("in Claude: /mcp → agi → Reconnect" if agent == "claude" else "reopen Codex")
            try:
                command = membership.resume_command(agent)
            except (OSError, ValueError):
                command = None
            return (f"  connection stopped{since} · {fix}" + (f" · if its window is closed: {command}" if command
                                                               else ""), "error")
        if agent != "claude" or not member.get("joined"):
            return None
        try:
            test = delivery_probe.status()
        except (OSError, ValueError):
            return None
        state = test.get("state")
        if state == "confirmed" and test.get("session") == member.get("session"):
            return (f"  delivery confirmed {duration(time.time() - test['confirmed_at'])} ago", "dim")
        if state in ("queued", "sent"):
            return ("  delivery test sent · waiting for Claude to answer it", "dim")
        if state == "expired":
            return ("  delivery test expired: messages may not reach Claude · is it started with the channel flag?",
                    "error")
        return ("  delivery unverified · /who test claude checks it", "dim")

    # --- the empty chat (#6027): who's set up, read before anything is written; one command each to connect -------
    def show_setup(self, quiet=False):
        """The setup list: each client's registration as it is now. `quiet`: only when no agent has ever joined
        this chat (the first start), and then without "Checking…" first."""
        def read():
            states = {client: connect.state(client) for client in ("claude", "codex")}
            members = {agent: membership.status(agent) for agent in ("claude", "codex")}
            return states, members
        if not quiet:
            self.note("Checking Claude Code and Codex…", sticky=True)
        self.in_background(read, lambda result: self.setup_shown(result, quiet))

    def setup_shown(self, result, quiet):
        if isinstance(result, Exception):
            if not quiet:
                self.note(f"Couldn't read the setup: {result}", "error")
            return
        states, members = result
        ever = {agent: member["session"] is not None for agent, member in members.items()}
        joined = {agent: member["joined"] for agent, member in members.items()}   # now, not ever (#6123)
        if quiet and any(ever.values()):    # at start, only for a chat no agent has ever joined
            return
        names = {"claude": "Claude Code", "codex": "Codex"}
        rows = [("No agent has joined this chat yet." if not any(ever.values()) else "Agents for this chat", "key")]
        for client, (kind, folder) in states.items():
            name = names[client]
            if kind == "missing":
                line = f"{name}: not installed here"
            elif kind == "none":
                line = f"{name}: not set up · /connect {client}"
            elif kind == "other":
                line = f"{name}: set up for another chat ({folder}) · /connect {client} moves it here"
            elif joined[client]:
                line = f"{name}: set up ✓ · joined ✓"
            elif ever[client]:
                line = f"{name}: set up ✓ · left the chat · /join {client}"
            else:
                line = f"{name}: set up ✓ · " + ("start it with the channel flag (/connect claude shows how) and say "
                                                  "\"join the chat\"" if client == "claude" else
                                                  "start or reopen it and say \"join the chat\"")
            rows.append((line, "dim"))
        self.notes = [(text, kind, UNTIL_KEY) for text, kind in rows]

    def connect_client(self, client):
        """/connect claude|codex: the registration this chat needs, through the client's own command; replacing one
        that points at another chat asks first (#6027)."""
        def check():
            return connect.state(client)

        def checked(result):
            if isinstance(result, Exception):
                self.note(f"Couldn't read {client}'s setup: {result}", "error")
                return
            kind, folder = result
            if kind == "missing":
                self.note(f"{client} isn't installed here, or not on your PATH", "error")
            elif kind == "other":
                self.ask(f"{client} is set up for another chat ({folder}). Point it at this one instead?",
                         lambda: run(result))
            else:
                run(result)

        def run(seen):
            lines = []

            def write():                    # the setup you confirmed, checked again just before writing (#6123)
                if connect.state(client) != seen:
                    return "changed"
                return connect.connect(client, out=lines.append)
            self.note(f"Connecting {client}…", sticky=True)
            self.in_background(write, lambda code: self.connected(client, code, lines))
        self.in_background(check, checked)

    def connected(self, client, code, lines):
        if code == "changed":
            self.note(f"{client}'s setup changed since you looked, so nothing was written · /connect shows it now",
                      "error", sticky=True)
            return
        if isinstance(code, Exception) or code != 0:
            self.note(" ".join(lines) or f"Not connected: {code}", "error", sticky=True)
            return
        text = "\n".join(lines)
        self.notes = [(" ".join(line.split()), "key" if n == 0 else "dim", UNTIL_KEY)
                      for n, line in enumerate(text.splitlines()) if line.strip()]

    def codex_row(self):
        """Codex's delivery, from its watcher's own state (Codex, #6117): what's wrong, and the one fix."""
        from agi import bridge
        try:
            health = bridge.delivery_health()
        except (OSError, ValueError):
            return None
        state, through = health["state"], health.get("through")
        contact = health.get("last_contact")
        since = f" · last contact {duration(time.time() - contact)} ago" if contact else ""
        if state == "unselected":
            return ("  not joined yet · start Codex and say \"join the chat\"", "dim")
        if state == "left":
            return ("  left the chat · /join codex brings it back", "dim")
        if state == "stopped":
            return ("  its watcher stopped · /join codex restarts it (its Codex window must be open)", "error")
        if state == "unreachable":
            try:
                command = membership.resume_command("codex")
            except (OSError, ValueError):
                command = None
            return (f"  connection stopped{since} · reopen the saved Codex session"
                    + (f": {command}" if command else ""), "error")
        if state == "failed":
            return (f"  the wake{f' for #{through}' if through else ''} failed · /join codex tries again", "error")
        if state == "queued":
            return (f"  woken{f' for #{through}' if through else ''} · waiting for its session to handle it", "dim")
        return None

    def delivery_test(self):
        """/who test claude: one test to the joined session; /who shows how it went (#6107)."""
        try:
            delivery_probe.request()
        except (OSError, ValueError) as error:
            self.note(f"Not tested: {error}", "error")
            return False
        self.note("Delivery test sent to Claude's session · /who shows when it answers")
        return True

    def show_board(self, kind="tasks"):
        """/tasks or /todo as notes until a key; the status thread keeps the one shown current (apply_status)."""
        self.board_kind = kind
        rows = self.todo_rows() if kind == "todo" else self.task_rows()
        self.notes = self.board = [(text, style, UNTIL_KEY) for text, style in rows]

    def todo_entries(self):
        """/todo in order: what waits on the user first, then each owner's, oldest first: (text, style, to-do or
        None for a heading or a message). A line leads with its number, age and any date, which a narrow window
        keeps; its next step follows and the whole of it is one Enter away (Codex, #4125)."""
        if self.todo_list is None:
            return [("Checking to-dos…", "dim", None)]
        if not self.todo_list:
            return [("Nothing promised for later: agents record it with agi todo before saying later", "dim", None)]
        yours = [todo for todo in self.todo_list if todo.get("next_actor") == store.USER]
        groups = [("You", store.USER, yours)] + [
            (owner, owner, [todo for todo in self.todo_list if todo not in yours and todo["owner"] == owner])
            for owner in sorted({todo["owner"] for todo in self.todo_list if todo not in yours})]
        entries = []
        for label, colour, todos in groups:
            if not todos:
                continue
            entries.append((f"{label} · {len(todos)}", colour if colour in self.attr else "dim", None))
            for todo in sorted(todos, key=lambda todo: (not todo.get("is_next"), timestamp(todo["created_at"]))):
                title, step = (" ".join(clean(todo[key]).split()) for key in ("title", "next_action"))
                up = "up next · " if todo.get("is_next") else ""          # the owner's pick (#4189, #4208)
                entries.append((f"  T{todo['id']} {self.todo_when(todo)} · {up}{title} · next: {step}",
                                todo["owner"] if up and todo["owner"] in self.attr else "dim", todo))
        return entries

    def todo_rows(self):
        return [(text, style) for text, style, _ in self.todo_entries()]

    @staticmethod
    def todo_when(todo, short=True):
        """How old a to-do is, how long since it moved and any date: "3d, still 2d · due 28 Nov 09:00"."""
        age, still = since_days(todo["created_at"]), since_days(todo["updated_at"])
        moved = timestamp(todo["updated_at"]) > timestamp(todo["created_at"]) + 60
        when = (f"{age}" if short else f"made {age} ago") + (f", still {still}" if moved else "")
        if todo.get("due_at") is not None:
            try:
                when += f" · due {datetime.fromtimestamp(todo['due_at']).astimezone().strftime('%d %b %H:%M')}"
            except (OverflowError, OSError, ValueError):
                when += " · due date set"
        return when

    def todo_detail(self, todo, width):
        """One to-do in full, wrapped: everything the list line cuts at a narrow width."""
        lines = [(" ".join(clean(todo["title"]).split()), todo["owner"] if todo["owner"] in self.attr else "dim")]
        if todo.get("is_next") and todo.get("result") is None:
            lines.append((f"up next for {todo['owner']}", "dim"))
        actor = todo.get("next_actor")
        done = [f"done · {' '.join(clean(todo['result']).split())}"] if todo.get("result") is not None else []
        fields = done + [f"next: {' '.join(clean(todo['next_action']).split())}",
                  f"{todo['project']} · owner {todo['owner']} · "
                  + (f"{'you' if actor == store.USER else actor} can act now" if actor else "waiting to start"),
                  self.todo_when(todo, short=False),
                  f"from #{todo['source_id']}" + (f" · see {todo['reference']}" if todo.get("reference") else "")]
        if wait := todo.get("wait_for"):
            fields.insert(1, "Waiting for " + todo_state.wait_label(wait))
        for field in fields:
            lines += [(line, "dim") for line in textwrap.wrap(field, max(10, width - 4), subsequent_indent="  ")]
        return lines

    def enter_todo(self):
        """/todo: the list below the transcript, the keyboard in it: ↑ ↓ choose, Enter opens one, Esc leaves."""
        self.show_board("todo")
        items = [todo["id"] for _, _, todo in self.todo_entries() if todo]
        self.todo_focus, self.todo_open, self.todo_scroll = (items[0] if items else None), None, 0

    def open_todo(self, number):
        """A clicked T number: that to-do in full, open or done; the list behind it for Esc (#4257)."""
        known = {todo["id"]: todo for todo in self.todo_all or ()}
        if number not in known:
            self.note(f"T{number} isn't a to-do here" if self.todo_all is not None else "Checking to-dos…", "dim")
            return
        self.enter_todo()
        self.todo_focus = self.todo_open = number
        self.todo_scroll = 0

    def leave_todo(self):
        self.todo_focus = self.todo_open = None
        self.todo_scroll = 0
        self.notes = []

    def todo_key(self, key):
        """A key while /todo has the keyboard: True if it was the list's."""
        self.todo_said = None               # what the last key said lasts until the next one
        items = [todo for _, _, todo in self.todo_entries() if todo]
        ids = [todo["id"] for todo in items]
        everything = {todo["id"]: todo for todo in self.todo_all or items}
        if self.todo_open not in everything:   # removed meanwhile (Codex, #4147): back to the list
            self.todo_open, self.todo_scroll = None, 0
        if self.todo_focus not in ids and self.todo_focus != self.todo_open:
            self.todo_focus = ids[0] if ids else None
        opened = everything.get(self.todo_open)
        if key == "\x01":                   # Ctrl+A: add one of your own (Codex, #6088). Not a letter: typed letters
            self.start_todo_add()            # start a message here (screen audit B2)
            return True
        if key in (curses.KEY_UP, curses.KEY_DOWN) and opened:   # a long one scrolls (Codex, #4138)
            self.todo_scroll = max(0, self.todo_scroll + (1 if key == curses.KEY_DOWN else -1))   # the view caps it
            return True
        if key in (curses.KEY_UP, curses.KEY_DOWN) and ids:
            here = ids.index(self.todo_focus) if self.todo_focus in ids else 0
            self.todo_focus = ids[(here + (1 if key == curses.KEY_DOWN else -1)) % len(ids)]
            return True
        if key == "n" and opened and opened["result"] is None and self.todo_open in ids:
            # Reserve n for a displayed item so typing can leave the list and return to the draft.
            if self.writer:                 # the lock and the journal's sync stay off the keyboard (Codex, #4239)
                self.writer.submit("todo-next", store.set_next_todo, store.USER, self.todo_focus)
                self.todo_said = (f"Marking T{self.todo_focus} next…", "dim")
            else:
                try:
                    self.next_todo_result(store.set_next_todo(store.USER, self.todo_focus), None)
                except (OSError, ValueError) as error:
                    self.next_todo_result(None, error)
            return True
        if key == "o" and opened:           # its report or checklist, in the desktop's app for it
            if opened.get("reference"):
                self.open_link(opened["reference"])
            else:
                self.note("This one links no report", "dim")
            return True
        if key in ("\r", curses.KEY_ENTER) and (self.todo_focus in ids or opened):
            if self.todo_open is None:
                self.todo_open, self.todo_scroll = self.todo_focus, 0
            else:                           # the second Enter: where it was promised
                source = opened["source_id"]
                self.leave_todo()
                self.go_to(source)
            return True
        self.leave_todo()                   # typing goes back to the box
        return False

    def request_check(self, argument):
        """/todo #N 30m [note], /todo #N gate PROJECT SHA [note], /todo #N off: a to-do that follows request N to
        whoever holds it and closes with it (Codex's store, T5655), replacing /remind on the screen (#5649 6a,
        Codex #5771)."""
        usage = "Usage: /todo #N 30m [note] · /todo #N gate PROJECT SHA [note] · /todo #N off"
        parts = argument.split(maxsplit=2)
        target = parts[0].removeprefix("#")
        if len(parts) < 2 or not target.isascii() or not target.isdigit():
            raise ValueError(usage)
        request, rest = int(target), parts[1:]
        if rest[0].lower() == "off":
            job = (_cancel_check, request)
        elif rest[0].lower() == "gate":
            gate = rest[1].split(maxsplit=2) if len(rest) > 1 else []
            if len(gate) < 2:
                raise ValueError("Usage: /todo #N gate PROJECT SHA [note]")
            job = (_add_check, request, gate[0], None, gate[1], gate[2] if len(gate) > 2 else None)
        else:
            delay = parse_duration(rest[0])
            if not delay:
                raise ValueError(usage)
            job = (_add_check, request, "AGI", time.time() + delay, None, rest[1] if len(rest) > 1 else None)
        if self.writer:
            self.writer.submit("todo-check", *job)
            self.note("Saving the check…")
        else:
            self.note(job[0](*job[1:]))
        self.request_status()

    def next_todo_result(self, item, error):
        """What marking a to-do next did, said where /todo is looking; the owner is the saved row's, not the list's."""
        if error:
            self.todo_said = (f"Not marked next · {error}", "error")
        elif item:
            self.todo_said = (f"T{item['id']} is {item['owner']}'s next", "dim")
        self.request_status()

    def task_rows(self, ended_shown=3):
        """/tasks: active work and annotated handoffs, then the latest few that ended."""
        if self.task_list is None:
            return [("Checking tasks…", "dim")]
        held, handoffs, ended = [], [], []
        for task in self.task_list:         # newest change first
            about = fit(" ".join(clean(task["text"]).split()), 48)
            since = self._since(task["event_ts"])
            due = task.get("reminder_due")
            gate = task.get("reminder_gate")
            if gate is not None:
                about = f"Waiting for {gate['project']} {gate['commit'][:7]} · {about}"
            elif due is not None:
                try:
                    when = datetime.fromtimestamp(due).astimezone()
                    label = when.strftime("%H:%M" if when.date() == datetime.now().date() else "%d %b %H:%M")
                    about = f"Reminder at {label} · {about}"
                except (OverflowError, OSError, ValueError):
                    about = f"Reminder set · {about}"
            if task["state"] == "claimed":
                doing = fit(" ".join(clean(task.get("doing") or "").split()), 48)
                stale = " · over a day" if held_over_a_day(task) else ""
                held.append(" · ".join(part for part in (f"#{task['id']} {task['owner']}{helpers_tag(task)} on it {since}{stale}",
                                                         f"▸ {doing}" if doing else "", about) if part))
            elif task["state"] == "released" and (task.get("note") or task.get("handoff_to") or gate is not None
                                                  or due is not None):
                named = task.get("handoff_to") or task.get("deferred_todos")
                note = " ".join(clean(task.get("note") or "").split())
                generated = "Waiting for " + ", ".join(f"T{number}" for number in task.get("deferred_todos") or ())
                if note == generated:           # the store's own note: the label already says it (#4406, #4415)
                    note = ""
                label = waiting_for(task) if named else "handoff"
                note = fit(note or ("" if named else "Waiting for an owner"), 60)
                handoffs.append(" · ".join(part for part in (f"#{task['id']} {label}", note, about) if part))
            elif len(ended) < ended_shown:
                who = f"{task['owner']} done ✓" if task["state"] == "done" else "released"
                result = f" · {fit(task['result'], 40)}" if task.get("result") else ""
                ended.append(f"#{task['id']} {who} {since} ago{result} · {about}")
        rows = held[::-1] + handoffs[::-1] + ended
        return [(text, "dim") for text in rows] or [("No requests taken yet: agents take one with agi take", "dim")]

    @staticmethod
    def _since(ts):
        try:
            return duration(max(0, time.time() - datetime.fromisoformat(ts).timestamp()))
        except ValueError:
            return "a while"

    def _timing(self, agent):
        """How fast `agent`'s wakes were queued and its messages handled, both from each one's log time.

        A message of yours unhandled past WAKE_LATE and not queued is a missed wake (red), but only while the
        guard lets agents be woken and the agent isn't mid-turn (an active turn holds new wakes on purpose).
        A queued one is only waiting for the agent to finish what it's doing."""
        try:
            timing = store.wake_timing(agent)
        except (OSError, ValueError):
            return "", "dim"
        waiting = timing.get("oldest_unacked")
        if waiting and (waiting.get("age") or 0) >= WAKE_LATE:
            busy = self.guard.get("blocked") or self.presence.get(agent, {}).get("state") == "working"
            missed = not waiting.get("queued") and not busy
            return (f" · your #{waiting['id']} unhandled {duration(waiting['age'])}, "
                    f"{'queued' if waiting.get('queued') else 'not queued'}", "error" if missed else "dim")
        last = timing.get("last") or {}
        times = [f"{label} {duration(last[key])}" for key, label in (("wake_s", "queued"), ("ack_s", "handled"))
                 if last.get(key) is not None]
        text = f" · #{last['id']} {', '.join(times)}" if times else ""
        handled = [row["ack_s"] for row in timing.get("recent", []) if row.get("ack_s") is not None]
        if len(handled) > 1:
            text += f" · slowest of {len(handled)} {duration(max(handled))}"
        return text, "dim"

    @staticmethod
    def _ago(message):
        try:
            seconds = time.time() - datetime.fromisoformat(message["ts"]).timestamp()
        except ValueError:
            return f"at #{message['id']}"
        return "just now" if seconds < 60 else f"{duration(seconds)} ago"

    def find(self, query):
        """Jump to the next older message containing `query`; repeat with a bare /find."""
        if not query and self.found:
            query, before = self.found
        elif query:
            before = None
        else:
            self.note("Usage: /find some words · /find again for older matches", "error")
            return
        needle = query.casefold()
        shown = self.messages if before is None else (self.view if self.view is not None else self.messages)
        for message in reversed(shown):     # a new search starts from the latest messages, even in history
            if before is not None and message["id"] >= before:
                continue
            if message["kind"] == "msg" and needle in message["text"].casefold():
                self.found = (query, message["id"])
                self.set_highlight(query)
                if shown is self.messages:
                    self.view = None
                self.jump_to(message["id"], query)
                self.note(f"Found #{message['id']} · /find for older · Esc to return")
                return
        oldest = shown[0]["id"] if shown else None
        hits = store.search_messages(query, limit=1, before_id=min(filter(None, (before, oldest)), default=None),
                                     kind="msg")    # only messages can be shown, not claims or notices
        older = hits[-1] if hits else None
        if older:                           # older than the screen holds: open a history view around it
            self.found = (query, older["id"])
            self.set_highlight(query)
            self.view = store.read_messages(max(0, older["id"] - 15), limit=40)
            self.jump_to(older["id"], query)
            self.note(f"Found #{older['id']} in history · /find for older · Esc returns to latest")
        else:
            self.found = None
            self.note(f"No older message contains \"{query}\"")

    def submit(self, text):
        """Send or run `text`. The draft is cleared only when this succeeds."""
        self.running = True
        command, argument = (text.split(maxsplit=1) + ["", ""])[:2]
        command, argument = command.casefold(), argument.strip()
        if command == "/reply":             # '/reply 42 text' becomes the message '↪ #42 text'
            target, rest = (argument.split(None, 1) + ["", ""])[:2]   # a new line after the number works too
            if not re.fullmatch(r"#?[0-9]{1,12}", target) or not rest.strip():
                self.note("Usage: /reply 42 your answer", "error")
                return
            text = f"↪ #{target.lstrip('#')} {rest.strip()}"
        changed = False             # something was written: refresh after, outside the send's error path
        try:
            if not text.lstrip().startswith("/") or text.startswith("//"):
                final = self.expand(text[1:] if text.startswith("//") else text)
                final = final.strip("\n").rstrip()   # a chip's own edge newlines too, as the contract says
                images = [dict(ref) for ref in self.images]
                extra = {"attachments": images} if images else {}   # a text message is sent as it always was
                if not final.strip() and not images:
                    return                          # a paste of only whitespace: nothing to send
                if self.writer:                 # write off the input thread; the draft stays until it commits
                    self.sending = self._draft_state()
                    if self.drafts:
                        self.writer.submit("send", functools.partial(self.drafts.send, **extra), final,
                                           self.draft, self.cursor, self.chips, self.chip_count)
                    else:
                        self.writer.submit("send", functools.partial(store.append_message, **extra),
                                           store.USER, final)
                    self.set_highlight(None)
                    self.notes = [n for n in self.notes if n[1] != "error"]
                    return
                if self.drafts:                 # journalled: a power cut can't duplicate or resurrect it
                    self.drafts.send(final, self.draft, self.cursor, self.chips, self.chip_count, **extra)
                    self.saved_state = ("", 0, {}, self.chip_count, [])
                else:
                    store.append_message(store.USER, final, **extra)
                self.images = []
                self.set_highlight(None)
                changed = True
            elif command == "/quit":
                self.running = False
            elif command in ("/custom", "/mode", "/pause", "/resume"):
                if not self.set_mode(command, argument):
                    return
                changed = True
            elif command in ("/claim", "/release"):
                if not argument:
                    self.note(f"Usage: {command} path/to/file", "error")
                    return
                store.append_message(store.USER, argument, command[1:])
                changed = True
            elif command == "/mouse" and argument not in ("on", "off"):
                self.note("Usage: /mouse on (scroll the chat) · /mouse off (select text normally)", "error")
                return
            elif command == "/mouse":
                self.set_mouse(argument == "on")
                try:                                # remembered, as /settings does (#6043)
                    ui_state.set_user_setting("mouse", argument == "on")
                except (OSError, ValueError):
                    pass                            # a convenience: the choice works now either way
                self.note("Scrolling moves the chat · drag selects, Ctrl+C copies" if self.mouse
                          else "Mouse off · normal selection · PgUp/PgDn scroll")
            elif command == "/sound" and argument not in sound.MODES:
                self.note(f"Sound is {sound.mode()} · /sound away · /sound always · /sound off",
                          "error" if argument else "dim")
                return
            elif command == "/sound":
                try:
                    sound.set_mode(argument)
                except OSError as error:
                    self.note(f"Couldn't save the sound setting: {error}", "error")
                    return
                self.note({"away": "Sound on while you're away from AGI", "always": "Sound on for every mention",
                           "off": "Sound off"}[argument])
            elif command == "/find":
                if "from:" in argument:              # one sender: the list only (the jump matches words alone)
                    self.show_found(argument)
                else:
                    self.keep_place()                # Tab comes back here, not to the newest match (#6096)
                    self.find(argument)              # jumps to the newest match, as before
                    if argument or self.found:
                        self.show_found(argument or self.found[0], before=self.found[1] if self.found else None,
                                        below=True)
            elif command == "/catchup":
                self.show_catchup()
            elif command == "/settings":
                self.show_settings()
            elif command == "/update":
                self.update_check()
            elif command == "/backup":
                self.note("Saving a backup…", sticky=True)
                self.in_background(backup.create, self.backup_saved)
            elif command == "/whatsnew":
                self.show_release()
            elif command == "/decisions":
                self.show_decisions()
            elif command == "/claims":
                self.note("; ".join(f"{path} → {who}" for path, who in sorted(self.claims.items()))
                          or "No active claims.", until_key=True)
            elif command == "/export":
                path = self.export(argument)
                if path is None:            # its note says why; the draft stays to fix (it vanished before)
                    return
                folder = os.path.dirname(path).replace(os.path.expanduser("~"), "~", 1)
                self.note(f"Saved {os.path.basename(path)} · in {folder}", sticky=True)   # the name survives a cut
            elif command == "/tasks":
                self.enter_rail() if self.rail_shown else self.show_board()
            elif command == "/todo" and (argument == "add" or argument.startswith("add ")):
                if not self.add_own_todo(argument[3:].strip()):
                    return
            elif command == "/todo" and argument:
                self.request_check(argument)
            elif command == "/todo":
                self.enter_todo()
            elif command == "/gates":
                self.show_gates()
            elif command == "/connect" and argument in ("claude", "codex"):
                self.connect_client(argument)
            elif command == "/connect":
                self.show_setup()
            elif command in ("/join", "/leave"):
                if not self.membership_command(command, argument):
                    return                          # the command stays in the box to fix, as others do
            elif command == "/unclaim":
                if not self.unclaim(argument):
                    return
            elif command == "/who" and argument == "test claude":
                if not self.delivery_test():
                    return
            elif command == "/who":
                self.notes = [(text, kind, UNTIL_KEY) for text, kind in self.who()]   # a row each, until a key
            elif command == "/help":
                self.help = True
            else:
                guess = difflib.get_close_matches(command, COMMANDS, n=1)
                hint = (f" · did you mean {guess[0]}?" if guess else " · ? lists commands") + \
                    " · start with // to send text beginning with /"
                self.note(f"Unknown command {command}{hint}", "error")
                return
        except (store.GuardBlocked, store.ClaimConflict, ValueError, OSError) as error:
            reason = error.strerror.lower() if isinstance(error, OSError) and error.strerror else str(error)
            self.note(f"Not sent: {reason} · your text is still here, press Enter to retry", "error", sticky=True)
            return
        if text not in self.history[-1:] and text.startswith("/"):
            self.history.append(text)
        if text.startswith("/"):            # running a command isn't reading what's on screen (#6113)
            self.interacted = False
        self.draft, self.cursor, self.history_pos, self.stash = "", 0, None, ""
        self.chips, self.stash_chips = {}, {}
        self.notes = [n for n in self.notes if n[1] != "error"]
        self.save_draft(now=True)           # a crash right after sending must not restore the sent text
        if changed:
            try:
                self.refresh_data()
            except (ValueError, OSError) as error:
                self.note(f"Sent · the screen couldn't refresh yet: {error}", "error", sticky=True)


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif")


def image_files(text):
    """The image files a paste names, or []: one per line (a path, or file:// as file managers drop them), or several
    quoted on one line, as xfce4-terminal drops them."""
    from urllib.parse import unquote, urlparse
    import shlex

    def image(word):
        word = word.strip()
        if word.startswith("file://"):
            word = unquote(urlparse(word).path)
        path = os.path.expanduser(word)
        return path if word and path.lower().endswith(IMAGE_SUFFIXES) and os.path.isfile(path) else None

    paths = []
    for line in text.strip().splitlines():
        whole = image(line.strip().strip("'\""))         # one path, spaces and all
        if whole:
            paths.append(whole)
            continue
        try:
            words = shlex.split(line)
        except ValueError:                  # an unmatched quote: not a list of files
            return []
        found = [image(word) for word in words]
        if not words or not all(found):
            return []
        paths += found
    return paths[:store.MAX_ATTACHMENTS]


def _copy(said, text):
    clipboard.copy(text)
    return said


def _read_clipboard():
    """("text", str), ("image", a stored image's reference) or None: on the writer thread, as storing an image is."""
    found = clipboard.read()
    if found and found[0] == "image":
        return "image", store.attach(found[1])
    return found


def _attach_file(path):
    data = pixbuf.file_png(path)
    if not data:
        raise ValueError(f"{os.path.basename(path)} couldn't be read as an image")
    return store.attach(data)


def _open_checks(request):
    return [item for item in store.list_todos() if item["source_id"] == request and item.get("follow_request")]


def _one_check(request):
    """The open check on request N, None when there's none; several (made with the CLI) aren't guessed between."""
    checks = _open_checks(request)
    if len(checks) > 1:
        numbers = ", ".join(f"T{item['id']}" for item in checks)
        raise ValueError(f"#{request} has several checks ({numbers}); change them in /todo")
    return checks[0] if checks else None


def _add_check(request, project, due_at, commit, note):
    """A to-do following request N: due at a time, or when the commit reaches main; an open one is rescheduled, not
    doubled, switching between a time and a commit (Codex, #5775). The said line, or ValueError."""
    holder = next((task["owner"] for task in store.list_tasks() if task["id"] == request and task["state"] == "claimed"),
                  None)
    if holder is None:
        raise ValueError(f"Nobody holds #{request} now; a check follows whoever holds it")
    wait = f"gate:{gate_reminders.project_id(project)}:{commit}" if commit else None
    # the shown name (Artlab), as list_todos gives it back; the gate target keeps the store's key (ArtLab; Codex, #5794)
    label = gate_reminders.display_name(gate_reminders.project_id(project)) if commit else project
    when = f"when {commit[:7]} reaches {label} main" if commit else f"at {time.strftime('%H:%M', time.localtime(due_at))}"
    existing = _one_check(request)
    if existing is not None:
        changes = {}                        # only what changes: the store refuses clearing what isn't set
        if due_at != existing.get("due_at"):
            changes["due_at"] = due_at
        if wait != existing.get("wait_for"):
            changes["wait_for"] = wait
        if label != existing.get("project"):        # an Artlab gate moves the item to Artlab (Codex, #5785)
            changes["project"] = label
        if note and (note, note) != (existing.get("title"), existing.get("next_action")):
            changes.update(title=note, next_action=note)
        if not changes:                             # the same check again: nothing to write, the store refuses it
            return f"T{existing['id']} already checks #{request} {when}"
        item = store.update_todo(store.USER, existing["id"], existing["revision"], **changes)
        return f"T{item['id']} now checks #{request} {when}"
    item = store.add_todo(store.USER, request, label, note or f"Check on #{request}", holder,
                          note or "See where it stands", next_actor=holder, due_at=due_at, wait_for=wait,
                          follow_request=True)
    return f"T{item['id']} checks #{request} {when}, with whoever holds it then"


def _cancel_check(request):
    check = _one_check(request)
    if check is None:
        raise ValueError(f"No open check on #{request}")
    store.complete_todo(store.USER, check["id"], check["revision"], "Cancelled")
    return f"Cancelled T{check['id']}"
