"""Measuring and wrapping terminal text with stdlib unicodedata only. Owner: Claude."""

import re
import unicodedata
from collections import namedtuple
from datetime import datetime


MENTION = re.compile(r"(?<![\w.])(@[\w-]+)")   # not inside an email address
ME = ("@user", "@you", "@all")
TASK = re.compile(r"^task:(\d+)$")
REPLY = re.compile(r"^↪ #(\d{1,12})(?!\d)\s*")   # '/reply 42 text' is stored as '↪ #42 text'; longer numbers are text
ISO = re.compile(r"\d{4}-\d\d-\d\dT(\d\d:\d\d)[\d:.]*(?:[+-]\d\d:\d\d|Z)?")



_widths = {}


def _joins(ch):
    """A character that belongs to the one before it: a combining mark, a variation selector, a skin tone or
    a tag (subdivision flags)."""
    return (unicodedata.category(ch) in ("Mn", "Me", "Mc") or "\ufe00" <= ch <= "\ufe0f"
            or "\U000e0100" <= ch <= "\U000e01ef" or "\U0001f3fb" <= ch <= "\U0001f3ff"
            or "\U000e0020" <= ch <= "\U000e007f")


PICTOGRAPHS = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2300, 0x23FF), (0x2B00, 0x2BFF))   # the emoji blocks


def _pictographic(ch):
    """Emoji enough to take part in a ZWJ sequence (👨‍👩‍👧, 🏳️‍🌈): the emoji blocks, or a BMP symbol. Not
    everything above U+1F000: paste chips (U+F0000 up) and CJK extension letters edit on their own
    (Codex, #1472)."""
    code = ord(ch)
    return any(low <= code <= high for low, high in PICTOGRAPHS) or (code < 0x10000 and unicodedata.category(ch) == "So")


def clusters(text, alone=()):
    """Where each visible character starts in `text`, plus its length: é as e + accent, 👍🏽, a ZWJ family and a
    flag are one each, so the cursor and deletes treat them whole (UI bug hunt, 2026-11-18). A small
    stand-in for Unicode's grapheme rules (UAX #29), enough for what people type and paste into a chat:
    line breaks and other controls are always their own (GB4/5), and a joiner links two pictographs only
    (GB11), so \"a‍b\" stays two letters (Codex, #1469). Characters in `alone` (the composer's paste chips)
    neither join nor take anything after them (#1474)."""
    starts, index = [], 0
    while index < len(text):
        starts.append(index)
        base = text[index]
        index += 1
        if unicodedata.category(base) == "Cc" or base in alone:
            if base == "\r" and text[index:index + 1] == "\n":
                index += 1
            continue                        # nothing joins a control, and it joins nothing
        if "\U0001f1e6" <= base <= "\U0001f1ff" and index < len(text) and "\U0001f1e6" <= text[index] <= "\U0001f1ff":
            index += 1                      # two regional indicators: one flag
        while index < len(text):
            ch = text[index]
            if ch in alone:
                break
            if _joins(ch):
                index += 1
            elif ch == "\u200d":
                index += 1                  # the joiner belongs here; what follows only if both are pictographs
                if (index < len(text) and _pictographic(base) and _pictographic(text[index])):
                    index += 1
            else:
                break
    starts.append(len(text))
    return starts


def cell_width(ch):
    """Terminal cells for one character, using only unicodedata."""
    width = _widths.get(ch)
    if width is None:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf") or "\U0001f3fb" <= ch <= "\U0001f3ff":
            width = 0                       # skin-tone modifiers join the emoji before them: 👍🏽 is 2 cells
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            width = 2
        else:
            width = 1
        _widths[ch] = width
    return width


def text_width(text):
    return sum(cell_width(ch) for ch in text)


def clean(text):
    """Make agent text safe to draw: no control characters, no ZWJ/VS16 that VTE misdraws."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = text.replace("\u200d", "").replace("\ufe0f", "")
    return "".join(ch if ch == "\n" or unicodedata.category(ch) != "Cc" else "\ufffd" for ch in text)


def fit(text, width):
    """Cut text to `width` cells, ending in … when cut."""
    if text_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        if used + cell_width(ch) > width - 1:
            break
        out += ch
        used += cell_width(ch)
    return out + "…"


RowInfo = namedtuple("RowInfo", "line joint lead")
RowInfo.__doc__ = """Where a wrapped row came from: its source line, what joins it to the row before (None: it starts
its line; " " for a space the wrap dropped; "" inside a word or after a space it kept), and the cells of indent and
hang the screen put in front of it."""


def wrap(first, indent, segments, width, first_width=None, hangs=(), info=None):
    """Wrap (text, attr) segments after the `first` segments; later rows start with `indent`.

    `first_width`, when given, limits only the first row (room for a right-aligned label). `hangs[n]`,
    when given, follows `indent` on the wrapped rows of source line n: a list item's hanging indent, a
    quote's bar, code's gutter. Returns a list of rows, each a list of (text, attr). `info`, when given,
    receives a RowInfo per row, so a selection can be copied as it was written.
    """
    limit = width if first_width is None else min(width, first_width)
    rows, row = [], list(first)
    start = text_width(indent)
    used = sum(text_width(text) for text, _ in first)
    fresh = True                            # no word yet on this logical line: its leading indent is kept
    source = 0                              # the source line being wrapped
    if info is not None:
        info.append(RowInfo(0, None, used))

    def newline(hard=False, joint=" "):
        nonlocal row, used, limit, fresh, start
        fresh = False
        rows.append(row)
        hang = list(hangs[source]) if not hard and source < len(hangs) else []
        if text_width(indent) + sum(text_width(text) for text, _ in hang) > width // 2:
            hang = []                       # a deep list in a narrow window: keep half the row for words
        row = ([(indent, 0)] if indent else []) + hang
        start = text_width(indent) + sum(text_width(text) for text, _ in hang)
        used, limit = start, width
        if info is not None:
            info.append(RowInfo(source + 1, None, start) if hard else RowInfo(source, joint, start))

    # Words first, across styles: "**bold**." or "(@user)" is one word in two or three styles, so its
    # punctuation never starts the next row on its own (UI bug hunt, 2026-11-18).
    tokens = []                             # ("newline",) | ("space", text, attr) | ("word", [(text, attr), …])
    for text, attr in segments:
        for number, line in enumerate(text.split("\n")):
            if number:
                tokens.append(("newline",))
            for piece in re.split(r"(\s+)", line):
                if not piece:
                    continue
                if piece.isspace():
                    tokens.append(("space", piece, attr))
                elif tokens and tokens[-1][0] == "word":
                    tokens[-1][1].append((piece, attr))   # no space since the last word: the same word
                else:
                    tokens.append(("word", [(piece, attr)]))
    for token in tokens:
        if token[0] == "newline":
            newline(hard=True)
            source += 1
            fresh = True
            continue
        if token[0] == "space":
            _, word, attr = token
            size = text_width(word)
            if (used > start or fresh) and used + size <= limit:
                row.append((word, attr))
                used += size
            continue
        parts = token[1]
        size = sum(text_width(text) for text, _ in parts)
        if used + size > limit and used > start:
            newline(joint="" if row and row[-1][0][-1:].isspace() else " ")   # the space it broke at, if dropped
        if used + size > limit:             # a word longer than a row: cut it, measuring each char once
            for text, attr in parts:
                begin, taken = 0, 0
                for index, ch in enumerate(text):
                    cells = cell_width(ch)
                    if used + taken + cells > limit and (index > begin or used > start):
                        if index > begin:
                            row.append((text[begin:index], attr))
                        newline(joint="")   # inside a word: nothing between the pieces
                        begin, taken = index, 0
                    taken += cells
                if text[begin:]:
                    row.append((text[begin:], attr))
                    used += taken
            fresh = False
            continue
        row.extend(parts)
        used += size
        fresh = False
    rows.append(row)
    return rows


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def parse_duration(text):
    """'2h', '90m', '1h30m', '45s', 'off' -> seconds, or None if unreadable."""
    text = text.strip().lower()
    if text in ("off", "0", "stop"):
        return 0
    parts = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", text)
    if not text or not parts or not any(parts.groups()):
        return int(text) * 60 if text.isdigit() else None
    hours, minutes, seconds = (int(value or 0) for value in parts.groups())
    return hours * 3600 + minutes * 60 + seconds


def helpers_tag(task):
    """Who else is working on a request in hand, after its driver: " +codex"."""
    if task.get("state") != "claimed":
        return ""
    return "".join(f" +{name}" for name in task.get("helpers") or () if name != task.get("owner"))


def waiting_for(task):
    """What a released request waits on, in a word (T4254): the agent it was handed to, the to-dos it was deferred
    into, or no owner yet."""
    if task.get("handoff_to"):
        return f"→ {task['handoff_to']}"
    if task.get("deferred_todos"):
        return "waits " + " ".join(f"T{number}" for number in task["deferred_todos"])
    return "no owner"


def held_over_a_day(task):
    """Whether a claim is old enough to use the attention colour."""
    try:
        claimed = datetime.fromisoformat(task["event_ts"])
    except (KeyError, TypeError, ValueError):
        return False
    return (datetime.now(claimed.tzinfo) - claimed).total_seconds() > 86400
