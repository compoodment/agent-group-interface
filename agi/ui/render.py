"""How the screen looks: Option A message blocks, activity row, footer and draw(). Owner: Claude."""

import curses
import functools
import os
import re
import sys
import textwrap
import time
import unicodedata
from datetime import datetime

from agi import __version__, admin_view, store
from agi.ui.composer import MENU_ROWS, QUIT_WINDOW, UNTIL_KEY
from agi.ui.text import (ISO, ME, MENTION, REPLY, TASK, cell_width, clean, duration, fit, held_over_a_day,
                         helpers_tag, text_width, waiting_for, wrap)


LINK_ROOT = os.path.realpath(os.path.expanduser(os.environ.get("AGI_LINK_ROOT", "~")))   # local links: inside
INLINE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|~~[^~\n]+~~|(?<![\w*])\*[^*\s][^*\n]*?\*(?![\w*])"
                    r"|(?<!\w)_[^_\s][^_\n]*?_(?!\w)|\[[^\[\]\n]+\]\((?:https?://|/|~/)[^)\s]+\)|https?://[^\s)>]+)")
# a link's label excludes '[' too: otherwise every '[' of a long unmatched run rescanned the rest (quadratic)
LINK = re.compile(r"\[([^\[\]\n]+)\]\(((?:https?://|/|~/)[^)\s]+)\)")   # [label](web address or local path)
ITALIC = getattr(curses, "A_ITALIC", curses.A_DIM)    # older curses builds: dim instead
STYLE_BITS = curses.A_BOLD | curses.A_UNDERLINE | ITALIC   # what a selection keeps of a span: its look, not its colour
COMPOSER_ROWS = 5           # the composer grows up to this many rows, then scrolls
FRAME_START = b"\x1b[?2026h\x1b[?25l"   # begin synchronized output, hide the cursor (unknown modes are ignored)
FRAME_END = b"\x1b[?25h\x1b[?2026l"
HELP = ["Enter send · Ctrl+J or Alt+Enter new line (continues 1. and - lists) · Tab completes names and commands",
        "↑↓ move a row, at the top or bottom jump to the start or end · Shift+↑↓ sent messages",
        "Scroll/PgUp/PgDn · drag selects, Ctrl+C copies · Esc to latest · Ctrl+W word · Ctrl+C ×2",
        "Ctrl+V pastes text or an image; a dropped file attaches · Backspace at the start drops the last",
        "/reply N · /find · /custom 10 1 60 · /mode normal|wait · /pause · /export"]
PLACEHOLDER = "Message claude and codex · @ to mention"
SHORTCUTS_HINT = "? for shortcuts · ctrl+j newline"   # once on screen: the bottom line, or the empty box when narrow


PARTICIPANTS = ("claude", "codex", "admin")     # the order marks are drawn in
ADMIN = store.ADMIN
POSTERS = (*store.AGENTS, ADMIN)                # senders drawn with a name header, like an agent
MARK_GLYPHS = {"claude": "✓", "codex": "✓", "admin": "✓"}   # one check, told apart by each reader's colour (#4212)
RAIL_WIDTH, RAIL_FROM = 26, 80
                                                # beside the chat from 80 columns; narrower keeps today's look
REF = re.compile(r"(?<![\w#&/])([#T])(\d{1,9})\b")
LEADING_NAMES = re.compile(r"^(?:@[\w-]+[\s,:]*)+")   # "@claude @codex: …" in front of a request's words
DELIVERED = "○"                                 # a hollow dot in the agent's colour: handed over, not yet acked


@functools.lru_cache(maxsize=1024)
def rail_words(text):
    """A request's words for the rail, on one line: its @names and reply quote in front left off."""
    return LEADING_NAMES.sub("", REPLY.sub("", " ".join(clean(text).split()))) or "…"


def glyph(name, acked):
    return MARK_GLYPHS[name] if acked else DELIVERED


@functools.lru_cache(maxsize=4096)
def recipients(sender, kind, text):
    """Who a message is for, in mark order: you write to both agents (and the admin when you say @admin); an
    agent's message goes to the agents it mentions, never itself. Nothing for chatter and system lines.
    Cached: the mention scan is the slow part and a message never changes."""
    if kind != "msg" or sender not in (store.USER, *POSTERS):
        return ()
    names = set(store.AGENTS) if sender == store.USER else set()
    if "@" in text:                         # the cheap test first: most messages mention no one
        names |= store.mentioned_agents(text) - {sender}
    if "@" in text and store.mentioned(text, ("admin",)) and sender != ADMIN:
        names.add("admin")
    return tuple(name for name in PARTICIPANTS if name in names)


class Meta(str):
    """A message's right-aligned '#id time', behind its read marks: one dim coloured glyph per participant it
    went to, a hollow dot once delivered, their check once acked ('○ ✓ #3190 11:30'). `marks` is a tuple of
    (name, acked). Reads as its plain text for width and clicks."""

    def __new__(cls, base, marks=()):
        glyphs = "".join(glyph(name, acked) + " " for name, acked in marks)
        value = super().__new__(cls, glyphs + base)
        value.marks, value.base = tuple(marks), base
        return value


class Mark(int):
    """A drawing attribute for what the screen adds inside a message's own rows (a bullet, a code gutter, a link's
    ↗ and site): drawn as any other, left out when part of a line is copied."""


class Source(list):
    """A transcript row drawn from a message's own words, which a selection copies: its segments, and where they
    came from. `text` is its source line as written, `line` that line's number, `joint` and `lead` as in
    text.RowInfo. Rows the screen makes up (names, quotes, rules, task lines) are plain lists and never copied."""


def _sourced(body, info, lines):
    rows = []
    for segments, (line, joint, lead) in zip(body, info):
        row = Source(segments)
        row.line, row.joint, row.lead = line, joint, lead
        row.text = lines[line] if line < len(lines) else ""
        rows.append(row)
    return rows


def _written(text):
    """A message's lines as written, as its rows number them: line breaks as clean() makes them, tabs kept, and no
    control characters, so a paste can't carry an escape sequence along."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return ["".join(ch for ch in line if ch == "\t" or unicodedata.category(ch) != "Cc") for line in lines]


def _clusters(segments):
    """(characters, attr, column, cells) for each visible character of a row: an accent stays with its letter."""
    column, out = 0, []
    for text, attr in segments:
        for ch in text:
            size = cell_width(ch)
            if not size and out:
                out[-1][0] += ch
                continue
            out.append([ch, attr, column, size])
            column += size
    return out


def _is_mark(attr):
    return isinstance(attr, Mark) or getattr(attr, "mark", False)


def _cut(row, cells):
    """A Source row's words within columns [start, end): (text, whether that starts at or before its first word,
    whether it reaches its last). Marks are left out, and so are the indent and hang in front of the row."""
    start, end = cells
    text, first, last = "", None, None
    for characters, attr, column, size in _clusters(row):
        if column < row.lead or _is_mark(attr):
            continue
        if not characters.isspace():
            first = column if first is None else first
            last = column + size
        if column + size > start and column < end:
            text += characters
    if first is None:                       # only marks or space: a blank line, a fence, a rule
        return "", True, True
    return text, start <= first, end >= last


def _rows(body, meta=None, band=False):
    """Screen rows from wrapped rows: '#id time' on the first, an optional tinted band on all."""
    return [(row, meta if number == 0 else None, band) for number, row in enumerate(body)]


class Renderer:
    """Message blocks, transcript rows and the whole-screen draw. Mixed into Screen."""
    notes_first = 0             # the index in self.notes of the first note row drawn (for a list's chosen row)

    left, rail_shown, gate_snapshot = 0, False, None    # until the first draw, and the first gates read
    drawn_rows, drawn_body = None, None                 # the transcript rows the last draw showed, and their height
    rail_sorted = (None, [])                            # the task list the rail last sorted, and its order
    rail_actions, rail_open = {}, None                  # what a click on each rail row does; the task opened there
    jumped_from = []                                    # (view, top message) before each jump, for Tab back
    rail_focus = None                                   # the task the keyboard is on in the rail (/tasks, #3302)
    todo_focus = None                                   # /todo: the to-do the keyboard is on (#4125)
    todo_open = None                                    # /todo: the one opened in full
    todo_scroll = 0                                     # /todo: how far down the open one is scrolled
    todo_said = None                                    # /todo: what the last key did, in place of the key line
    todo_all = None                                     # every to-do, done ones too (set by apply_status)
    row_offsets = []                                    # each shown row's place in its message's rows (selection)
    selection = None                                    # (anchor, end) of a mouse selection: (id, row, column) each
    press = None                                        # (y, x, spot) of a press on the transcript, until released
    drag_mode = None                                    # the drag-reporting request for the terminal, until sent

    # -- message rendering --------------------------------------------------

    def _inline(self, text, attr, out):
        """`code`, **bold**, *italic*, ~~struck~~, links and @mentions inside one line of prose."""
        for part in INLINE.split(text):
            if not part:
                continue
            link = LINK.fullmatch(part)
            if part.startswith("`") and part.endswith("`") and len(part) > 2:
                out.append((part[1:-1], self.attr["code"]))
            elif part.startswith("**") and part.endswith("**") and len(part) > 4:
                self._inline(part[2:-2], attr | curses.A_BOLD, out)
            elif part.startswith("~~") and part.endswith("~~") and len(part) > 4:
                self._inline(part[2:-2], attr | curses.A_DIM, out)   # curses has no strikethrough: faded
            elif len(part) > 2 and part[0] == part[-1] and part[0] in "*_":
                self._inline(part[1:-1], attr | ITALIC, out)
            elif link:                      # [text](url or path): the words underlined, a click opens it; a
                site = re.match(r"https?://([^/]+)", link.group(2))   # web link names its site, so a label
                target = link.group(2)                                 # can't hide where it goes
                hint = site.group(1) if site else link_hint(target)   # a file says what it is before it's opened
                out += [(link.group(1), LinkAttr(attr | curses.A_UNDERLINE, target)),
                        (f" ↗ {hint}" if hint else " ↗", LinkAttr(self.attr["dim"], target, mark=True))]
            elif part.startswith(("http://", "https://")):
                out.append((part, LinkAttr(attr | curses.A_UNDERLINE, part)))
            else:
                for piece in MENTION.split(part):
                    if not piece:
                        continue
                    if MENTION.fullmatch(piece):
                        name = piece.lower()[1:]
                        out.append((piece, self.attr["me"] if piece.lower() in ME else
                                    self.attr.get(f"{name}_mention", attr | curses.A_BOLD)))
                    elif REF.search(piece):   # "#3433" goes there (#3458); "T4157" opens that to-do (#4257)
                        at = 0
                        for found in REF.finditer(piece):
                            if found.start() > at:
                                out.append((piece[at:found.start()], attr))
                            kind = "message" if found.group(1) == "#" else "todo"
                            out.append((found.group(0), LinkAttr(attr | curses.A_UNDERLINE, (kind, int(found.group(2))))))
                            at = found.end()
                            target = (getattr(self, "by_id", None) or {}).get(int(found.group(2))) if kind == "message" else None
                            if target and piece[:found.start()].endswith("↪ "):   # an answer inside a message:
                                mine = target["sender"] == store.USER              # whose, as the first line's
                                out.append((" " + ("you" if mine else target["sender"]),   # quote says (#4304)
                                            Mark(self.attr["me"] if mine else self.attr["dim"])))
                        if at < len(piece):
                            out.append((piece[at:], attr))
                    elif self.highlight:        # /find matches stand out until Esc or the next send
                        for bit in re.split(f"({re.escape(self.highlight)})", piece, flags=re.IGNORECASE):
                            if bit:
                                out.append((bit, self.attr["found"] if bit.casefold() == self.highlight.casefold()
                                            else attr))
                    else:
                        out.append((piece, attr))

    def _spans(self, text, attr=0, hangs=None):
        """Light markdown: fenced code, inline styles, - and 1. lists, [ ] tasks, > quotes, # headings, ---.

        `hangs`, when given, receives what each line's wrapped rows continue with (see `wrap`)."""
        out, fenced, hangs = [], False, [] if hangs is None else hangs
        for number, line in enumerate(clean(text).split("\n")):
            if number:
                out.append(("\n", attr))
            hangs.append([])
            if line.lstrip().startswith("```"):
                fenced = not fenced
                label = line.strip()[3:].strip()
                out.append((f"── {label} " if fenced and label else "──", Mark(self.attr["dim"])))
                continue
            if fenced:
                out += [("│ ", Mark(self.attr["dim"])), (line, self.attr["code"])]
                hangs[-1] = [("│ ", self.attr["dim"])]   # a wrapped code line keeps its gutter
                continue
            heading = re.match(r"#{1,4} (.*)", line)
            task = re.match(r"(\s*)[-*] \[([ xX])\] (.*)", line)
            bullet = re.match(r"(\s*)[-*] (.*)", line)
            numbered = re.match(r"(\s*)(\d{1,4}[.)]) (.*)", line)
            quote = re.match(r"\s*> ?(.*)", line)
            if heading:
                self._inline(heading.group(1), attr | curses.A_BOLD | curses.A_UNDERLINE, out)
            elif re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
                out.append(("─" * 12, Mark(self.attr["dim"])))
            elif task:
                out.append((task.group(1) + ("☑ " if task.group(2) != " " else "☐ "), Mark(self.attr["dim"])))
                hangs[-1] = [(" " * text_width(task.group(1) + "☐ "), 0)]   # wrapped words line up after the mark
                self._inline(task.group(3), attr | curses.A_DIM if task.group(2) != " " else attr, out)
            elif bullet:
                out.append((bullet.group(1) + "• ", Mark(self.attr["dim"])))
                hangs[-1] = [(" " * text_width(bullet.group(1) + "• "), 0)]
                self._inline(bullet.group(2), attr, out)
            elif numbered:
                out.append((f"{numbered.group(1)}{numbered.group(2)} ", self.attr["dim"]))
                hangs[-1] = [(" " * text_width(f"{numbered.group(1)}{numbered.group(2)} "), 0)]
                self._inline(numbered.group(3), attr, out)
            elif re.match(r"Skills:\s", line):
                self._inline(line, self.attr["dim"], out)
            elif quote:                     # a quoted line: a dim bar, the words in italics
                out.append(("▎ ", Mark(self.attr["dim"])))
                hangs[-1] = [("▎ ", self.attr["dim"])]
                self._inline(quote.group(1), attr | ITALIC, out)
            else:
                self._inline(line, attr, out)
        return out

    def _meta(self, message):
        try:
            stamp = datetime.fromisoformat(message["ts"]).strftime("%H:%M")
        except ValueError:
            stamp = "--:--"
        return Meta(f"#{message['id']} {stamp}", self.marks.get(message["id"], ()))

    def faint(self, name):
        """A participant's colour, quiet: read marks are dim, not bold like their names."""
        return (self.attr[name] & ~curses.A_BOLD) | curses.A_DIM

    @staticmethod
    def _folded(message, by_id):
        """A task claim or release shown under its request instead of as a row of its own. A change to what the owner
        is doing never gets a row, even once its request has scrolled out of the history kept: the rail and /tasks
        show it (Codex, #5282)."""
        task = TASK.match(message["text"]) if message["kind"] in ("claim", "release") else None
        if task and message["kind"] == "claim" and message.get("doing_update"):
            return True
        return bool(task) and int(task.group(1)) in by_id

    def update_marks(self):
        """Work out every shown message's marks from the cursors and wake receipts the status thread read: a dot
        once handed to an agent (a wake receipt covers it), its check once its ack reaches the message; the admin
        has no dot, its inbox is read-only. Only messages whose marks change need to be wrapped again."""
        marks = {}
        for message in self.messages:
            number = message["id"]
            row = []
            names = recipients(message["sender"], message["kind"], message["text"])
            if ADMIN not in names and "reply_to" in message and admin_view.addressed_to_admin(message, lookup=self.by_id.get):
                names += (ADMIN,)
            for name in names:
                if self.acks.get(name, 0) >= number:
                    row.append((name, True))
                elif name != "admin" and any(first <= number <= last for first, last in self.wakes.get(name, ())):
                    row.append((name, False))
            if row:
                marks[number] = tuple(row)
        for number in self.marks.keys() | marks.keys():
            if self.marks.get(number) != marks.get(number):
                self.cache.pop(number, None)
        self.marks = marks

    def _proto_text(self, message):
        sender, kind, text = message["sender"], message["kind"], clean(message["text"])
        if kind == "claim":
            return f"{sender} claimed {text}", "dim"
        if kind == "release":
            return f"{sender} released {text}", "dim"
        todo_line = self._todo_text(message, text)
        if todo_line:
            return todo_line, "dim"
        text = ISO.sub(lambda match: match.group(1), text)
        text = re.sub(r"(\d+) seconds", lambda m: f"{int(m.group(1)) // 60} min" if int(m.group(1)) % 60 == 0
                      else duration(int(m.group(1))), text)
        text = text.replace("Agent loop guard: ", "Loop guard: ").replace("Agent rate guard: ", "Rate limit: ")
        text = text.replace("Autonomous mode enabled until", "Auto mode on until")
        text = text.replace("Autonomous mode disabled", "Auto mode off").replace(" agent messages per ", " agent messages / ")
        text = text.replace("; ", " · ").rstrip(".")
        return text, ("error" if self._is_refusal(message) else "dim")

    @staticmethod
    def _refs(text, attr):
        """A system line's #N and T numbers as links, the rest plain: the ◷ lines open their to-do (Codex, #4280)."""
        out, at = [], 0
        for found in REF.finditer(text):
            if found.start() > at:
                out.append((text[at:found.start()], attr))
            kind = "message" if found.group(1) == "#" else "todo"
            out.append((found.group(0), LinkAttr(attr | curses.A_UNDERLINE, (kind, int(found.group(2))))))
            at = found.end()
        return out + ([(text[at:], attr)] if at < len(text) or not out else [])

    @staticmethod
    def _todo_text(message, text):
        """A to-do's record as one short line after its ◷: what was promised and by whom,
        that it's done and how, or whose next it is. None for any other record."""
        event, selection = message.get("todo_event"), message.get("todo_next")
        if event:
            changes, number = event["changes"], event["id"] or message["id"]
            title = text.split(" · ", 1)[1] if " · " in text else text      # as the store worded it
            if message.get("todo_ready"):
                return f"T{number} ready · next for {changes['next_actor']}"
            if event["id"] is None:
                due = changes.get("due_at")
                when = ""
                if due is not None:
                    try:
                        when = f" · due {datetime.fromtimestamp(due).astimezone().strftime('%d %b %H:%M')}"
                    except (OverflowError, OSError, ValueError):
                        when = " · due date set"
                return f"T{number} {changes['owner']} · {title}{when}"
            if set(changes) == {"result"}:
                return f"T{number} done · {' '.join(clean(changes['result']).split())}"
            return f"T{number} changed · {title}"
        if selection:
            if selection["todo_id"] is None:
                return f"{selection['owner']}'s next cleared"
            return f"T{selection['todo_id']} is {selection['owner']}'s next"
        return None

    def block(self, message, width):
        """Rows for one message, as (segments, right_text, band). Cached per width."""
        rows = self.cache.get(message["id"])
        if rows is not None:
            return rows
        meta = self._meta(message)
        room = width - text_width(meta) - 2
        sender, kind = message["sender"], message["kind"]
        text, quote = self._reply(message, width) if kind == "msg" else (message["text"], [])
        if kind == "msg" and message.get("attachments"):    # each image as a link that opens it full size
            links = "\n".join(f"[▣ image · {ref['width']}×{ref['height']}]({store.attachment_path(ref)})"
                              for ref in message["attachments"])
            text = f"{text}\n{links}" if text.strip() else links
        if kind == "msg" and sender == store.USER:
            arrow = self.attr.get("user_band", self.attr["user"])
            hangs, info = [], []
            body = wrap([("❯ ", arrow)], "  ", self._spans(text, hangs=hangs), room, hangs=hangs, info=info)
            rows = quote + _rows(_sourced(body, info, _written(text)), meta, band=True)
            events = self.tasks.get(message["id"], [])
            if events:
                rows.append(([("  └  ", self.attr["dim"]),
                              (self._task_summary(events), self.attr["dim"])], None, False))
        elif kind == "msg" and sender in POSTERS:
            header = [("● " + sender, self.attr[sender])]
            mine = self.addressed_to_me(message)    # marked by the bar below, not a badge as well
            rows = [(header, meta, False)] + quote
            # the first row leaves room for '#id time', so a grouped message can drop its header
            hangs, info = [], []
            body = wrap([("  ", 0)], "  ", self._spans(text, hangs=hangs), width, first_width=room, hangs=hangs,
                        info=info)
            if mine:                        # a bar down the left edge marks messages addressed to you
                body = [[("▌", self.attr["user"]), (row[0][0][1:], row[0][1])] + row[1:] for row in body]
            rows += _rows(_sourced(body, info, _written(text)))
        elif self._folded(message, self.by_id):
            rows = []                       # folded under the request it refers to
        else:
            text, kind_name = self._proto_text(message)
            symbol = ("⚑ " if kind in ("claim", "release")    # not └: these don't answer the row above
                      else "◷ " if message.get("todo_event") or message.get("todo_next") else "· ")
            info = []
            body = wrap([(symbol, self.attr["dim"])], "  ", self._refs(text, self.attr[kind_name]), room, info=info)
            rows = _rows(_sourced(body, info, text.split("\n")), meta)
        self.cache[message["id"]] = rows
        return rows

    def _reply(self, message, width):
        """Split '↪ #42 text' into the text and a dim one-row quote of #42 (just '#42' if not on screen)."""
        match = REPLY.match(message["text"])
        if not match:
            return message["text"], []
        target = self.by_id.get(int(match.group(1)))
        quote = [("  ", 0), (f"↪ #{match.group(1)}", self.attr["dim"])]
        if target:
            mine = target["sender"] == store.USER
            name = "you" if mine else target["sender"]
            snippet = " ".join(REPLY.sub("", clean(target["text"])).split())
            used = 2 + text_width(quote[1][0]) + 1 + text_width(name) + 2
            quote += [(" ", 0), (name, self.attr["me"] if mine else self.attr["dim"]),
                      (": " + fit(snippet, max(1, width - used - 2)), self.attr["dim"])]
        return message["text"][match.end():], [(quote, None, False)]

    def addressed_to_me(self, message):
        """An @user/@all mention, or a reply to one of the user's own messages: the store's one rule, resolved
        against the messages on screen so the check never reads the disk."""
        return store.addressed_to(message, store.USER, lookup=self.by_id.get)

    @staticmethod
    def mentions_me(message):
        """The store's rule (not in code, quotes or after a backslash), for the names that mean the user."""
        return bool(store.mentioned(message["text"], tuple(name[1:] for name in ME)))

    @staticmethod
    def _task_summary(events):
        """Who took a request and where it stands: on it, done, or released (handed back, not marked done)."""
        held, order, ended = {}, [], None
        for kind, agent, _ in events:
            if kind == "claim":
                held[agent] = True
                order.append(agent)
            elif agent in held:
                held[agent], ended = False, kind
        names = " → ".join(dict.fromkeys(order))
        if any(held.values()):
            return f"{names} is on it" if len(order) == 1 else f"{names} (on it)"
        return f"{names} took this · {'done ✓' if ended == 'done' else 'released'}"

    def _date_rule(self, message, width):
        try:
            label = datetime.fromisoformat(message["ts"]).strftime("%a %d %b %Y")
        except ValueError:
            label = message["ts"][:10]
        return self._rule(label, self.attr["rule"], width)

    def _rule(self, label, attr, width):
        """A full-width ─── label ─── row."""
        label = f" {label} "
        side = max(2, (width - text_width(label)) // 2)
        return ([("─" * side + label + "─" * (width - side - text_width(label)), attr)], None, False)

    def blocks_backward(self, width):
        """Yield (message, rows) from newest to oldest; rows include the separator above the message."""
        if width != self.cache_width:
            self.cache.clear()
            self.cache_width = width
            self.selection = None           # its rows are numbered for the old width
        messages = self.view if self.view is not None else self.messages
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            block = self.block(message, width)
            if not block:
                continue
            previous = None
            for back in range(index - 1, -1, -1):
                if self.block(messages[back], width):
                    previous = messages[back]
                    break
            if (self.view is None and previous is not None and self.new_after is not None
                    and previous["id"] <= self.new_after < message["id"]):
                day = [self._date_rule(message, width)] if previous["ts"][:10] != message["ts"][:10] else []
                # a new day that starts where you stopped reading keeps its date (times show only HH:MM)
                separator = [([], None, False), *day,       # a click on it opens /catchup (#6054)
                             self._rule("new", LinkAttr(self.attr["user"], ("catchup", 0)), width),
                             ([], None, False)]
            elif previous is None or previous["ts"][:10] != message["ts"][:10]:
                separator = [([], None, False), self._date_rule(message, width), ([], None, False)]
            elif self._continues(previous, message):    # the same agent again: a blank line, no repeated name
                separator, block = [([], None, False)], self._without_header(block, width)
            elif previous["kind"] == "msg" or message["kind"] == "msg":   # protocol rows sit together
                separator = [([], None, False)]
            else:
                separator = []
            yield message, separator + block

    GROUP_SECONDS = 180             # an agent's messages this close together share one header

    def _continues(self, previous, message):
        """True when `message` continues `previous`: same agent, both plain messages, close in time."""
        if not (message["kind"] == previous["kind"] == "msg" and message["sender"] == previous["sender"]
                and message["sender"] in POSTERS and not self.addressed_to_me(message)):
            return False
        try:
            gap = datetime.fromisoformat(message["ts"]) - datetime.fromisoformat(previous["ts"])
        except (ValueError, TypeError):     # unparsable, or naive mixed with aware timestamps
            return False
        return 0 <= gap.total_seconds() <= self.GROUP_SECONDS

    def _without_header(self, block, width):
        """Drop the '● sender' row, moving '#id time' onto the first text row when it fits."""
        header, body = block[0], block[1:]
        meta = header[1]
        if not body or not meta:
            return block
        segments, _, band = body[0]
        if sum(text_width(text) for text, _ in segments) + text_width(meta) + 2 > width:
            return block                    # no room: keep the header so the id stays visible
        return [(segments, meta, band)] + body[1:]

    def body_rows(self, width, height):
        """Rows ending at the bottom of the transcript, enough for `height` + scroll."""
        need = height + self.scroll
        rows, owners, offsets, exhausted = [], [], [], True
        for message, block in self.blocks_backward(width):
            rows[:0] = block
            owners[:0] = [message["id"]] * len(block)
            offsets[:0] = range(len(block))
            if len(rows) >= need:
                exhausted = False
                break
        if exhausted:
            self.scroll = max(0, min(self.scroll, len(rows) - height))
        end = len(rows) - self.scroll
        self.row_owners = owners[max(0, end - height):end]   # which message each shown row belongs to (clicks)
        self.row_offsets = offsets[max(0, end - height):end]  # and where in its rows: a selection stays on its words
        return rows[max(0, end - height):end]                 # while the chat scrolls or grows


    def spot(self, y, x, clamp=False):
        """The transcript cell at screen (y, x) as (message id, row in its block, column), or None off the
        transcript. `clamp` (while dragging): above it is its first row's start, below it its last row's end."""
        index, column = y - self.rows_top, x - self.left
        count = len(self.shown_rows)
        if not count or not clamp and (column < 0 or not 0 <= index < count):
            return None
        if index < 0:
            index, column = 0, 0
        elif index >= count:
            index, column = count - 1, sys.maxsize
        return self.row_owners[index], self.row_offsets[index], max(0, column)

    def selected_cells(self, key):
        """The columns [start, end) the selection covers on the row `key` (message id, row), or None."""
        if not self.selection:
            return None
        first, last = sorted(self.selection)
        if not first[:2] <= key <= last[:2]:
            return None
        start = first[2] if key == first[:2] else 0
        end = last[2] + 1 if key == last[:2] else sys.maxsize      # the cell under the pointer is in it
        return (start, end) if start < end else None

    def selected_text(self):
        """The selection as written: a line selected whole is its source line, markdown and all; part of a line
        is the words shown, without bullets, gutters or a link's ↗; wrapped rows join back into their line. Only a
        message's own words: never names, times, marks, quotes or the rail. Messages are a blank line apart."""
        if not self.selection:
            return ""
        first, last = sorted(self.selection)
        messages = []
        for message, block in self.blocks_backward(self.cache_width or self.chat_width()):
            if message["id"] < first[0]:
                break
            if message["id"] > last[0]:
                continue
            lines, group = [], []
            for offset, (segments, _, _) in enumerate(block):
                if not isinstance(segments, Source):
                    continue
                if group and segments.joint is None:
                    lines.append(self._copied_line(group))
                    group = []
                group.append((segments, self.selected_cells((message["id"], offset))))
            if group:
                lines.append(self._copied_line(group))
            lines = [line for line in lines if line is not None]
            if lines:
                messages.append("\n".join(lines))
        return "\n\n".join(reversed(messages))

    @staticmethod
    def _copied_line(group):
        """One source line's text from its rows and what each has selected, or None when none of it is."""
        cuts = [(row, _cut(row, cells)) for row, cells in group if cells]
        if not cuts:
            return None
        whole = len(cuts) == len(group) and cuts[0][1][1] and cuts[-1][1][2]
        if whole:
            return group[0][0].text
        text = ""
        for number, (row, (piece, _, _)) in enumerate(cuts):
            text += (row.joint or "") + piece if number else piece
        return text.rstrip() or None

    def clicked(self, y, x):
        """A click on the transcript: '#N time' starts '/reply N', a quoted '↪ #N' line jumps to #N. On the rail:
        a task jumps to its request and shows all its words, a person what they're on, the gates their detail."""
        if self.rail_shown and x < RAIL_WIDTH:
            return self.rail_clicked(self.rail_actions.get(y))
        index, x = y - self.rows_top, x - self.left
        for row, start, end, kind, number in getattr(self, "note_spots", ()):   # a number in the notes: /tasks's
            if y == row and start <= x < end:                                 # "waits T4326", a #request
                self.open_todo(number) if kind == "todo" else self.go_to(number)
                return True
        spot = getattr(self, "hint_spot", None)
        if spot and y == spot[0] and spot[1] <= x < spot[2]:   # "▼ 3 new · Esc to jump": go to the latest
            self.jump_to_latest()
            return True
        if x < 0 or not 0 <= index < len(self.shown_rows):
            return False
        segments, right, _ = self.shown_rows[index]
        width = self.chat_width()
        if right and x >= width - text_width(right) - 1:
            self.start_reply(self.row_owners[index])
            return True
        quoted = next((REPLY.match(text + " ") for text, _ in segments if text.startswith("↪ #")), None)
        if quoted:
            self.go_to(int(quoted.group(1)))
            return True
        column = 0
        for text, attr in segments:         # a link's words or its ↗ carry where it points, through wrapping
            column += text_width(text)
            if x < column:
                target = getattr(attr, "target", None)
                if isinstance(target, tuple) and target[0] == "catchup":   # the new line: what waits for you
                    self.show_catchup()
                    return True
                if isinstance(target, tuple):   # a message number: there, as a quote does; a to-do: opened
                    self.open_todo(target[1]) if target[0] == "todo" else self.go_to(target[1])
                    return True
                if target:
                    self.open_link(target)
                    return True
                break
        return False

    def rail_clicked(self, action):
        if not action:
            return False
        if action[0] == "task":
            self.rail_open = action[1]      # its words open in the rail until another task or Esc
            self.go_to(action[1])
        elif action[0] == "todo":
            self.enter_todo()
        elif action[0] == "person":         # all of it, not the rail's cut: their task, time and note
            lines = textwrap.wrap(action[1], max(10, self.chat_width() - 3), subsequent_indent="  ")
            self.notes = [(line, "dim", UNTIL_KEY) for line in lines]
        else:
            self.show_gates()
        return True

    def go_to(self, message_id):
        """Show message #N: on screen if loaded, else a history view around it (Esc returns to the latest, Tab to
        where you were)."""
        here = (self.view, self.row_owners[0] if self.scroll and self.row_owners else None)
        if not self.jumped_from or self.jumped_from[-1] != here:
            self.jumped_from = (self.jumped_from + [here])[-20:]   # a few jumps deep, as a browser's Back
        if not self.jump_to(message_id):
            self.view = store.read_messages(max(0, message_id - 15), limit=40)
            if not self.jump_to(message_id):
                self.view = None
                self.note(f"#{message_id} isn't in the chat log")
                return
        self.note(f"#{message_id} · Tab goes back · Esc to latest")

    def go_back(self):
        """Tab after a jump, when it has nothing to fill in: back to where you were reading, a step at a time (#3458,
        #3506: with text in the box too)."""
        view, top = self.jumped_from.pop()
        self.view = view
        self.interacted = False             # coming back isn't reading what's there: the read marker stays (#6103)
        if top is None or not self.jump_to(top):
            self.scroll = 0                 # you were at the latest (or that spot is gone): the latest
        self.note("Back where you were" + (" · Tab again" if self.jumped_from else ""))

    def jump_to(self, message_id, needle=None):
        """Scroll so the message's first row sits at the top of the transcript. False if not on screen.
        The next draw settles it against the real height: the note this jump adds (Found #N) takes a row,
        which cut the message's header off when measured now (UI bug hunt, 2026-11-18)."""
        rows = self.body_height - (0 if self.hint_shown else 1)   # once scrolled, the hint takes a row
        scroll = self._scroll_to(message_id, rows, needle)
        if scroll is None:
            return False
        self.scroll, self.pinned = scroll, (message_id, needle)
        return True

    @staticmethod
    def _match_row(block, needle):
        """The row a match starts on, even when wrapping split the phrase across rows (Codex, #1447)."""
        words = needle.casefold().split()
        if not words:
            return None
        flat, starts = "", []
        for segments, _, _ in block:
            starts.append(len(flat))
            flat += " ".join("".join(text for text, _ in segments).casefold().split()) + " "
        found = flat.find(" ".join(words))
        if found < 0:
            return None
        return max(n for n, start in enumerate(starts) if start <= found)

    def _scroll_to(self, message_id, rows, needle=None, *, width=None):
        """The scroll that puts the message's first row at the top of `rows` transcript rows, or None. With a
        `needle` further down than fits, its row shows instead, two rows below the top."""
        below = 0
        for message, block in self.blocks_backward(width or self.cache_width or 80):
            below += len(block)
            if message["id"] == message_id:
                separator = next((n for n, row in enumerate(block) if row[0]), 0)
                down = 0
                match = self._match_row(block, needle) if needle else None
                if match is not None and match - separator >= rows - 1:
                    context = max(0, min(2, rows - 1))   # rows above the match, as many as a short screen allows
                    down = match - separator - context
                return max(0, below - separator - rows - down)
        return None

    # -- drawing ----------------------------------------------------------

    def chat_width(self):
        """The transcript's and composer's columns: the terminal's, less the rail's when it shows."""
        return self.win.getmaxyx()[1] - self.left

    def _put(self, y, x, text, attr=0):
        """Text at (y, x) of the chat's columns (right of the rail), cut at the terminal's edge."""
        x += self.left
        height, width = self.win.getmaxyx()
        if 0 <= y < height and 0 <= x < width:
            try:
                self.win.addstr(y, x, fit(text, width - x) if text_width(text) > width - x else text, attr)
            except curses.error:            # the bottom-right cell
                pass

    def _band_attr(self, attr, band):
        """A style as it looks on your messages' tinted band: a coloured word keeps its colour on the band."""
        if not (band and self.attr["band"]):
            return attr
        pair = curses.pair_number(attr & curses.A_COLOR)
        if not pair:
            return attr | self.attr["band"]
        if pair in self.on_band:
            return (attr & ~curses.A_COLOR) | curses.color_pair(self.on_band[pair])
        return attr

    def _put_row(self, y, row, width):
        segments, right, band = row
        if band and self.attr["band"]:
            self._put(y, 0, " " * width, self.attr["band"])
        x = 0
        for text, attr in segments:
            self._put(y, x, text, self._band_attr(attr, band))
            x += text_width(text)
        if right:
            x = width - text_width(right)
            for name, acked in getattr(right, "marks", ()):     # read marks: one quiet coloured glyph each
                self._put(y, x, glyph(name, acked), self._band_attr(self.faint(name), band))
                x += 2
            base = getattr(right, "base", right)
            self._put(y, x, base, self.attr["meta"] | (self.attr["band"] if band else 0))

    def _put_selected(self, y, row, cells):
        """Reverse video over the selected part of a row's own words, as a terminal shows a selection; never over
        the indent, name or time the screen adds, which Ctrl+C leaves out."""
        segments, _, _ = row
        start, end = cells
        for characters, attr, column, size in _clusters(segments):
            if column >= segments.lead and column + size > start and column < end:
                # one colour for the whole selection: a coloured span (`code`, a link) reversed kept its own
                self._put(y, column, characters, attr & STYLE_BITS | curses.A_REVERSE)

    def activity(self, width):
        if not self.presence:               # status hasn't arrived yet: don't claim anyone is away
            return [("○ checking who's here…", self.attr["dim"])]
        parts = []
        now = time.time()
        for agent in store.AGENTS:
            # turn is running (its hooks or bridge report it, with a lease), "waiting" when it holds a task
            # between turns, "connected" after work reporting expires, "idle" when reachable with nothing held,
            # and "away" when it isn't reachable.
            info = self.presence.get(agent, {})
            state = info.get("state", "unknown")
            held = sorted(int(path[5:]) for path, who in self.claims.items()   # a file claim that only looks
                          if who == agent and path.startswith("task:")                         # like one is skipped
                          and path[5:].isascii() and path[5:].isdigit())                       # ("task:²" too)
            # the newest task is the one in hand; older ones still held are counted, not shown
            task = f"#{held[-1]}" + (f" +{len(held) - 1}" if len(held) > 1 else "") if held else ""
            since = info.get("since")
            if state == "working":
                text = f"✻ {agent} working" + (f" · {task}" if task else "") + \
                    (f" · {duration(now - since)}" if since else "")
                attr = self.attr[agent]
            elif state == "unknown" and info.get("reachable"):
                text = f"○ {agent} connected" + (f" · {task}" if task else "")
                attr = self.attr["dim"]
            elif info.get("reachable", state in ("idle", "listening")):
                text = f"○ {agent} waiting · {task}" if task else f"○ {agent} idle"
                attr = self.attr[agent] if task else self.attr["dim"]
            else:
                text, attr = f"○ {agent} away" + (f" · {task}" if task else ""), self.attr["dim"]
            member = info.get("member") or {}
            if member.get("session") and not member.get("joined"):    # left by choice: not the same as away (#6020)
                text, attr = f"○ {agent} left" + (f" · {task}" if task else "") + f" · /join {agent}", self.attr["dim"]
            elif member.get("joined") and not member.get("reachable") and state != "working":
                # joined, but its connection stopped: say so, and where the fix is (the audit, #6130)
                text, attr = (f"○ {agent} connection stopped" + (f" · {task}" if task else "") + " · /who",
                              self.attr["error"] if "error" in self.attr else self.attr["dim"])
            if member.get("pending") and member.get("pending_reachable"):
                text += f" · another session wants to join: /join {agent}"
            if info.get("note"):            # one row: a note's line breaks become spaces (UI bug hunt)
                text += f" · {' '.join(clean(info['note']).split())}"
            parts.append((text, attr))
        return parts

    def jump_hint(self, width):
        """The row over the box while reading older messages: how many new ones are still below, and from whom."""
        count, senders = len(self.unseen), ", ".join(dict.fromkeys(self.unseen.values()))
        if self.view is not None:
            waiting = f" · {count} new" if count else ""
            return self._hint(width, f" History view{waiting} · Esc returns to latest ", f" History{waiting} · Esc ")
        if self.scroll and self.unseen:
            return self._hint(width, f" ▼ {count} new · {senders} · Esc to jump ",
                              f" ▼ {count} new · Esc to jump ", f" ▼ {count} new · Esc ")
        if self.scroll:
            return self._hint(width, " ▼ Esc to jump to latest ", " ▼ Esc ")
        return ""

    @staticmethod
    def _hint(width, *options):
        """The first wording that fits, longest first; the shortest is cut as a last resort."""
        return next((text for text in options if text_width(text) <= width), None) or fit(options[-1], width)

    @staticmethod
    def _share(parts, width, gap=3):
        """Cut the longest (text, attr) parts so all of them fit on one row, each keeping its start."""
        room = width - gap * (len(parts) - 1)
        left, out = len(parts), {}
        for number in sorted(range(len(parts)), key=lambda n: text_width(parts[n][0])):
            cap = min(text_width(parts[number][0]), max(1, room // left))
            out[number] = (fit(parts[number][0], cap), parts[number][1])
            room, left = room - cap, left - 1
        return [out[number] for number in range(len(parts))]

    def footer(self):
        guard = self.guard
        mode = guard.get("mode")
        if mode is None:
            return "checking mode…", self.attr["dim"]
        if mode in ("normal", "auto", "custom"):    # rate modes: how much of the window the agents have used
            label = "custom" if mode == "auto" else mode
            window = guard.get("window_seconds") or 60
            per = "min" if window == 60 else f"{window // 60} min" if window % 60 == 0 else f"{window} s"
            left = f"{label} · {guard.get('used', 0)}/{guard.get('message_limit')} per {per}"
            if label == "custom" and guard.get("expires_at"):
                left += f" until {datetime.fromtimestamp(guard['expires_at']).strftime('%H:%M')}"
            if guard.get("blocked") and self.retry_left() > 0:
                return f"{left} · full, agents wait {duration(self.retry_left())}", self.attr["error"]
            return left, self.attr["dim"]
        if mode in ("quiet", "paused"):
            return "paused · agents can't post · /resume", self.attr["error"]
        if mode == "autonomous":                # the older store's timed mode
            left = f"auto until {datetime.fromtimestamp(guard['expires_at']).strftime('%H:%M')}"
            if guard.get("blocked") and self.retry_left() > 0:
                left += f" · agents rate-limited for {duration(self.retry_left())}"
                return left, self.attr["error"]
            return left, self.attr["dim"]
        if guard.get("blocked"):
            if self.refused:
                return "agents paused · your turn", self.attr["error"]
            return "your turn", self.attr["dim"]
        return f"agents {guard.get('agent_streak', 0)}/{guard.get('limit', store.GUARD_LIMIT)} before your turn", self.attr["dim"]

    def draw(self):
        self.win.erase()
        height, width = self.win.getmaxyx()
        if height < 8 or width < 30:
            self._put(0, 0, "Please enlarge the terminal")
            self._show()
            return
        now = time.monotonic()
        # the message a reply answers stays while the draft is that reply (screen audit B4: it went at the first key)
        self.notes = [n for n in self.notes if (n[2] is None or n[2] > now)
                      and (n[1] != "quote" or self.draft.startswith("/reply"))]
        full = width
        self.rail_shown = width >= RAIL_FROM
        self.left = 0
        if self.rail_shown:
            self.draw_rail(height - 1)
            self.left = RAIL_WIDTH + 1
            width -= self.left
        lines, (cursor_row, cursor_col) = self.composer_lines(width)
        options = self.menu()
        if options:                         # the / menu: matching commands, the selected one highlighted
            want = min(len(options), MENU_ROWS)
        else:
            want = len(HELP) + 1 if self.help else len(self.notes)
            if self.todo_focus is not None:     # /todo: its list and hint, or one to-do in full; the room caps it
                want = max(want + 1, 12)
        # a single note goes on the rule above the composer, as "sending…" does, so nothing above moves: with a row
        # of its own, the chat jumped up a row and back when it cleared, and a click right after landed a row off
        rule_note = None
        # the message a reply answers stays a row of its own, just above the box: long, on the rule it read as part of
        if (not options and not self.help and self.todo_focus is None and len(self.notes) == 1
                and self.notes[0][1] != "quote" and not (self.notes == self.board or self.gates_visible())):
            rule_note, want = self.notes[0][:2], 0
        # footer, 2 rules, the activity row (none beside the rail: it shows the same) and a hint are fixed; the
        # transcript keeps at least 1 row, so the composer shrinks first (its cursor row stays visible), then the
        # menu/notes
        hint = None if getattr(self, "confirming", None) else self.jump_hint(width)
        activity = not self.rail_shown
        room = height - 4 - activity - bool(hint)
        shown = max(1, min(len(lines), COMPOSER_ROWS, room - want))
        want = min(want, room - shown)
        top = max(0, min(cursor_row - shown + 1, len(lines) - shown))
        if options:
            selected = min(self.menu_index, len(options) - 1)
            first = max(0, min(selected - want + 1, len(options) - want))
            note_rows = []
            for number in range(first, first + want):
                choice = options[number]
                marker = "› " if number == selected else "  "
                line = f"{marker}{choice.title} · {choice.description}"
                note_rows.append((fit(line, width - 2), "menu_on" if number == selected else "dim"))
        elif self.help:
            note_rows = [(fit(line, width - 2), "dim") for line in (HELP + [f"AGI {__version__}"])[:want]]
        else:
            # On a short terminal, keep the first task or gate rows visible.
            board_visible = (self.notes == self.board or self.gates_visible()
                             or self.notes == getattr(self, "release_board", None)    # /whatsnew reads from the top
                             or self.notes == getattr(self, "decision_board", None))  # and /decisions
            first = 0 if board_visible else max(0, len(self.notes) - want)
            if self.pick is not None and want > 0:  # a list's chosen row stays in view in a short window (#6096)
                first = min(max(first, self.pick - want + 1), self.pick)
            visible = self.notes[first:first + want]
            self.notes_first = first
            if self.notes == getattr(self, "release_board", None) and len(self.notes) > want > 1:
                visible = visible[:want - 1] + [(f"… {len(self.notes) - want + 1} more lines: a taller window shows "
                                                 "them, or CHANGELOG.md", "dim", None)]
            note_rows = [(fit(text, width - 2), kind) for text, kind, _ in visible]
            if self.todo_focus is not None:
                note_rows = self.todo_view(width, want)
        images = self.image_row(width)       # the draft's images, just above it
        if images:
            note_rows = note_rows + [(images, "user")]
        # the hint gets a row of its own, outside the messages: drawn over them, it scrolled along with them
        # (curses moves lines in the terminal) and then jumped back, which flickered
        bottom = 1 + 1 + shown + 1 + activity + len(note_rows) + bool(hint)   # footer, rules, composer, activity, notes
        body = max(1, height - bottom)
        self.body_height, self.hint_shown = body, bool(hint)
        if self.pinned is not None:          # a jump: measured again now the notes and hint are in place
            (message_id, needle), self.pinned = self.pinned, None
            scroll = self._scroll_to(message_id, body, needle, width=width)
            self.scroll = self.scroll if scroll is None else scroll
        rows = self.body_rows(width, body)
        if self.unseen and any(self.row_owners):   # New messages scrolled into view stop counting.
            newest = max(owner for owner in self.row_owners if owner)
            self.unseen = {message_id: sender for message_id, sender in self.unseen.items() if message_id > newest}
            hint = self.jump_hint(width) if hint else hint   # same row, the count as it is now
        if self.rail_shown and (rows, body) != (self.drawn_rows, self.drawn_body):
            # A terminal scrolls whole rows: curses would scroll the moved transcript, or the box and notes
            # "scrolling ... flickers the left panel"). Forgetting what the lines hold leaves curses nothing to
            # scroll: it writes them in place.
            self.win.redrawln(0, height)
        self.drawn_rows, self.drawn_body = rows, body
        y = body - len(rows)
        self.rows_top, self.shown_rows = y, rows
        for index, row in enumerate(rows):
            self._put_row(y, row, width)
            cells = self.selection and isinstance(row[0], Source) and self.selected_cells(
                (self.row_owners[index], self.row_offsets[index]))
            if cells:
                self._put_selected(y, row, cells)
            y += 1
        self.hint_spot = None
        if hint:
            x = max(0, (width - text_width(hint)) // 2)
            self._put(body, x, hint, curses.A_REVERSE)
            self.hint_spot = (body, x, x + text_width(hint))
        y = body + bool(hint)
        self.note_spots = []                # where each #N and T number in the notes was drawn, for a click (T4254)
        for number, (text, kind) in enumerate(note_rows):
            chosen = self.pick is not None and self.pick == self.notes_first + number   # the list's chosen row
            self._put(y, 2, text, self.attr["menu_on"] if chosen else self.attr[kind])
            self.note_spots += [(y, *spot) for spot in self.note_links(text, 2)]
            y += 1
        if activity:
            x = 0
            for text, attr in self._share(self.activity(width), width):
                self._put(y, x, text, attr)
                x += text_width(text) + 3
            y += 1
        self._put(y, 0, "─" * width, self.attr["rule"])
        # the eye already is (#5509): sending…, a second-press warning, then a note
        x = 1
        status = []
        if self.sending:
            status.append(("sending…", self.attr["sending"]))
        if time.time() - self.ctrl_c_at < QUIT_WINDOW:
            status.append(("ctrl+c again to quit", self.attr["dim"]))
        elif self.draft and time.time() - self.esc_at < QUIT_WINDOW:
            status.append(("esc again to clear", self.attr["dim"]))
        elif self.esc_hint():
            status.append((self.esc_hint(), self.attr["dim"]))
        for text, attr in status:
            self._put(y, x, f" {text} ", attr)
            x += text_width(text) + 3
        if rule_note is None and getattr(self, "whatsnew", None) and x + 6 < width:   # until /whatsnew or Esc (T4270)
            rule_note = (f"new in AGI {self.whatsnew['version']} · /whatsnew · Esc hides it", "key")
        if rule_note is not None and x + 6 < width:
            text = fit(rule_note[0], width - x - 3)
            self._put(y, x, f" {text} ", self.attr[rule_note[1]])
            self.note_spots += [(y, *spot) for spot in self.note_links(text, x + 1)]
        y += 1
        composer_top = y
        for number in range(shown):
            index = top + number
            prefix = "❯ " if index == 0 else "  "
            self._put(y, 0, prefix, self.attr["user"])
            self._put(y, 2, lines[index], 0)
            y += 1
        footer_has_hint = text_width(self.footer()[0]) + text_width(SHORTCUTS_HINT) + 6 <= full
        if not self.draft:
            placeholder = PLACEHOLDER if footer_has_hint else "? for shortcuts · " + PLACEHOLDER   # first: cut last
            self._put(composer_top, 2, fit(placeholder, width - 3), self.attr["dim"])
        self._put(y, 0, "─" * width, self.attr["rule"])
        y += 1
        chat_left, self.left, width = self.left, 0, full     # the footer runs under the rail too
        left, left_attr = self.footer()
        hints = SHORTCUTS_HINT
        self._put(y, 2, left, left_attr)
        if text_width(left) + text_width(hints) + 6 <= width:
            self._put(y, width - text_width(hints) - 1, hints, self.attr["dim"])
        self.left = chat_left
        self.win.move(composer_top + cursor_row - top, min(full - 1, chat_left + 2 + cursor_col))
        self._show()

    def draw_rail(self, height):
        """The rail's `height` rows at the left and its dividing line: people, then tasks, then gates; tasks give
        up rows first when they don't all fit."""
        people, tasks, gates, todo = self.rail_people(), self.rail_tasks(), self.rail_gates(), self.rail_todo()
        room = height - len(people) - (len(gates) + 1 if gates else 0) - len(todo) - 2   # blank row and "tasks"
        kept, shown = [], 0
        for task in tasks:                  # whole tasks only: a header and its words (all of them, opened)
            rows = self.rail_task(*task)
            left = len(tasks) - shown - 1   # after this one: a row to say how many more, if any
            if len(kept) + len(rows) + (left > 0) > room:
                break
            kept += rows
            shown += 1
        dim = self.attr["dim"]
        more = [(f"+{len(tasks) - shown} more · /tasks", dim, None)] if shown < len(tasks) else []
        empty = "checking…" if self.task_list is None else "none in hand"
        rows = (people + [("", 0, None), ("tasks", dim, None)] + (kept or ([] if more else [(empty, dim, None)]))
                + more + todo)
        if gates:
            rows += [("", 0, None)] + gates
        self.rail_actions = {}              # what a click on each row does (clicked)
        for y, (text, attr, action) in enumerate(rows[:height]):
            self._put(y, 0, fit(text, RAIL_WIDTH - 1), attr)
            if action:
                self.rail_actions[y] = action
        for y in range(height):
            self._put(y, RAIL_WIDTH, "│", self.attr["rule"])

    def rail_people(self):
        """Each agent as the activity row says it, its task and time on the line below; the admin's last read."""
        dim = self.attr["dim"]
        rows = [("people", dim, None)]
        for text, attr in self.activity(RAIL_WIDTH):
            head, _, rest = text.partition(" · ")
            rows.append((head, attr, ("person", text)))
            if rest:
                rows.append(("  " + rest, dim, ("person", text)))
        if "admin" in self.acks:
            rows.append((f"{MARK_GLYPHS['admin']} admin read #{self.acks['admin']}", self.faint("admin"), None))
        return rows

    def rail_tasks(self):
        """Requests in hand, then handoffs, then the last three done, newest first in each, so a full rail cuts
        the oldest: (task, who, colour) for each; only the ones that fit are drawn (rail_task)."""
        if self.task_list is None:
            return []
        if self.rail_sorted[0] is self.task_list:   # the status thread replaces the list; a keystroke doesn't
            return self.rail_sorted[1]
        held, handoffs, ended = [], [], []
        dim = self.attr["dim"]
        for task in self.task_list:         # newest change first; drawn each frame, so only shown ones get words
            if task["state"] == "claimed":
                # Use the attention colour for older claims.
                stale = held_over_a_day(task)
                held.append((task, task["owner"], self.attr.get(store.USER if stale else task["owner"], 0)))
            elif task["state"] == "released" and (task.get("note") or task.get("handoff_to")
                                                  or task.get("reminder_gate") is not None
                                                  or task.get("reminder_due") is not None):
                handoffs.append((task, waiting_for(task), dim))
            elif task["state"] == "done" and len(ended) < 3:
                ended.append((task, f"✓ {task['owner']}", dim))
        self.rail_sorted = (self.task_list, held + handoffs + ended)
        return self.rail_sorted[1]

    def esc_hint(self):
        """What Esc will do, on the bottom rule, where that isn't obvious (#6059): the same order escape() follows."""
        if getattr(self, "confirming", None):
            return None                     # the question itself says "Esc no"
        if getattr(self, "selection", None):
            return "esc clears the selection"
        if getattr(self, "todo_open", None) is not None:
            return "esc back to the list"
        if getattr(self, "todo_focus", None) is not None or getattr(self, "rail_focus", None) is not None:
            return "esc back to typing"
        return None

    STALE_CONTACT = 30 * 60     # seconds without contact before a claim says so (#6043)

    def out_of_touch(self, task):
        """How long a claim's owner has been out of touch, when that's over STALE_CONTACT and it isn't reachable;
        else None. Nothing is released by itself: a disconnect can come while a test is still running (#6042)."""
        if task["state"] != "claimed":
            return None
        member = (getattr(self, "presence", {}).get(task["owner"]) or {}).get("member") or {}
        contact = member.get("last_contact")
        if member.get("reachable") or not contact or time.time() - contact < self.STALE_CONTACT:
            return None
        return duration(time.time() - contact)

    def rail_task(self, task, who, attr):
        """One task's rows: its number, who and how long ago, then its words on one row; the one clicked last
        shows them all (up to six rows), its header marked."""
        action, words = ("task", task["id"]), rail_words(task["text"])
        header = (f"#{task['id']} {who}{helpers_tag(task)} {self._since(task['event_ts'])}", attr, action)
        if task["id"] == self.rail_focus:   # the keyboard is here: marked as a menu's choice is
            header = (header[0], self.attr["menu_on"], action)
        doing = " ".join(clean(task.get("doing") or "").split()) if task["state"] == "claimed" else ""
        quiet = self.out_of_touch(task)
        if quiet:                           # the owner went quiet: the fix where the problem is (#6043)
            words_rows = [(fit(f"  away {quiet}: /unclaim", RAIL_WIDTH - 1), self.attr.get(store.USER, attr),
                           action)]           # the number is in the header just above; the rail is 26 wide
        else:
            words_rows = []
        if task["id"] != self.rail_open:
            return ([header] + ([("  ▸ " + doing, attr, action)] if doing else []) + words_rows
                    + [("  " + words, self.attr["dim"], action)])
        lines = lambda text, lead: ["".join(piece for piece, _ in row)
                                    for row in wrap([(lead, 0)], "  ", [(text, 0)], RAIL_WIDTH - 1)]
        rows = [(line, attr, action) for line in lines(doing, "  ▸ ")[:3]] if doing else []
        return ([(header[0], self.attr["menu_on"], action)] + rows
                + [(line, self.attr["dim"], action) for line in lines(words, "  ")[:6]])

    def todo_view(self, width, want):
        """/todo's rows in the notes area: one to-do in full, or the list scrolled to keep the chosen one in view,
        marked as a menu's choice is (#4125: a long list at 80 columns was cut to its first rows)."""
        entries = self.todo_entries()
        opened = {todo["id"]: todo for todo in self.todo_all or [t for _, _, t in entries if t]}.get(self.todo_open)
        rule = [self.todo_rule(width)]
        want -= 1
        if opened:                          # all of it reachable: it scrolls, and says how much more there is
            rows = self.todo_detail(opened, width)
            room = max(1, want - 1)
            self.todo_scroll = min(self.todo_scroll, max(0, len(rows) - room))
            shown = rows[self.todo_scroll:self.todo_scroll + room]
            below = len(rows) - self.todo_scroll - len(shown)
            scrolls, report = len(rows) > room, bool(opened.get("reference"))
            more = f"↓ {below} more · " if below else ""
            hint = more + " · ".join((["↑ ↓ scroll"] if scrolls else []) + [f"Enter goes to #{opened['source_id']}"]
                                     + (["o opens the report"] if report else [])
                                     + (["n makes it next"] if opened["result"] is None else []) + ["Esc back"])
            if text_width(hint) > width - 2:   # narrow: every key still named, in short
                hint = more + " · ".join((["↑↓"] if scrolls else []) + [f"Enter #{opened['source_id']}"]
                                         + (["o report"] if report else [])
                                         + (["n next"] if opened["result"] is None else []) + ["Esc"])
            said = self.todo_said or (hint, "dim")    # what the last key did, when it said something
            return rule + [(fit(text, width - 2), style) for text, style in shown] + [(fit(said[0], width - 2), said[1])]
        hint = self.todo_said or ("↑ ↓ choose · Enter opens it · Ctrl+A adds one · Esc back to typing", "dim")
        lines = [(("› " if todo and todo["id"] == self.todo_focus else "") + text,
                  "menu_on" if todo and todo["id"] == self.todo_focus else style) for text, style, todo in entries]
        room = max(1, want - 1)
        chosen = next((at for at, (_, _, todo) in enumerate(entries) if todo and todo["id"] == self.todo_focus), 0)
        first = max(0, min(chosen - room + 1, len(lines) - room))
        return rule + [(fit(text, width - 2), style) for text, style in lines[first:first + room]] + [hint]

    def todo_rule(self, width):
        """"── to-do · 1 for you · 13 later ───…", in the screen's rule colour, across the notes area."""
        todos = self.todo_list or []
        yours = sum(1 for todo in todos if todo.get("next_actor") == store.USER)
        label = " · ".join(["── to-do"] + ([f"{yours} for you"] if yours else []) + [f"{len(todos) - yours} later"]) + " "
        return (fit(label + "─" * max(0, width - 2 - text_width(label)), width - 2), "rule")

    @staticmethod
    def note_links(text, x):
        """The #N and T numbers a drawn note shows whole, as (start, end, kind, number) from column `x`: a number
        cut short by the width isn't one (Codex, #4406: "T41…" isn't T4179)."""
        spots = []
        for found in REF.finditer(text):
            if text[found.end():found.end() + 1] == "…":   # cut right after it: maybe not the whole number (#4415)
                continue
            start = x + text_width(text[:found.start()])
            spots.append((start, start + text_width(found.group(0)),
                          "message" if found.group(1) == "#" else "todo", int(found.group(2))))
        return spots

    def rail_todo(self):
        """Under the tasks, two short lines with the user’s ready to-dos, in their colour,
        only when there are some; and how many wait on the agents, with the command that lists them all."""
        if not self.todo_list:
            return []
        dim = self.attr["dim"]
        yours = sum(1 for todo in self.todo_list if todo.get("next_actor") == store.USER)
        rows = [("", 0, None)]
        if yours:
            rows.append((f"you {yours} · /todo", self.attr.get(store.USER, 0), ("todo",)))
        if len(self.todo_list) > yours:
            rows.append((f"later {len(self.todo_list) - yours} · /todo", dim, ("todo",)))
        return rows

    def rail_gates(self):
        """Each project's gate: what it's doing, whose change, who's queued. Nothing until the first look."""
        if not self.gate_snapshot:
            return []
        dim = self.attr["dim"]
        rows = [("gates", dim, ("gates",))]
        for project, stage, detail, queued in self.gate_snapshot:
            rows.append((f"{project} {stage.lower()}", 0 if stage != "Idle" else dim, ("gates",)))
            if any(detail):
                rows.append(("  " + " ".join(filter(None, detail)), dim, ("gates",)))
            if queued:
                rows.append(("  queued " + ", ".join(queued), dim, ("gates",)))
        return rows

    def _show(self):
        """Send the frame as one update: the cursor hides while it moves around (a clock ticking once made it
        blink there every second), and terminals with synchronized output show the frame all at once."""
        os.write(sys.stdout.fileno(), FRAME_START)
        self.win.noutrefresh()
        curses.doupdate()
        if self.drag_mode:                  # after curses' own mouse request, which would otherwise replace it
            os.write(sys.stdout.fileno(), self.drag_mode.encode())
            self.drag_mode = None
        os.write(sys.stdout.fileno(), FRAME_END)


class LinkAttr(int):
    """A drawing attribute that also knows where its link points. Wrapping keeps each piece's attribute as it
    is, so every piece of a link's words, and its ↗, opens that link's own target: two links with the same
    words, or a link's text quoted in code, can't be mistaken for each other (Codex, #1488)."""

    def __new__(cls, attr, target, mark=False):
        value = super().__new__(cls, attr)
        value.target, value.mark = target, mark      # mark: the ↗ and site, which copying a part leaves out
        return value


IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_hints = {}                 # path -> (modified time, hint): files are read once per change, not once per frame


def link_hint(target):
    """What a local link points to, shown beside its ↗ so results can be checked without opening them: an image's size ("▣ 1100×680"), a Markdown report's title, "missing" for a file that's gone. Never an
    error: anything unreadable shows no hint."""
    path = os.path.realpath(os.path.expanduser(target))
    try:
        modified = os.stat(path).st_mtime_ns
    except OSError:
        return "missing"
    cached = _hints.get(path)
    if cached and cached[0] == modified:
        return cached[1]
    hint = ""
    try:
        with open(path, "rb") as file:
            head = file.read(4096)
        lower = path.lower()
        if lower.endswith(".png") and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            hint = f"▣ {int.from_bytes(head[16:20], 'big')}×{int.from_bytes(head[20:24], 'big')}"
        elif lower.endswith(IMAGES):
            hint = "▣ image"
        elif lower.endswith(".md"):
            title = next((line[2:].strip() for line in head.decode("utf-8", "replace").splitlines()
                          if line.startswith("# ")), "")
            hint = title[:48] + ("…" if len(title) > 48 else "")
    except (OSError, IsADirectoryError):
        hint = ""
    _hints[path] = (modified, hint)
    return hint


def safe_target(target):
    """A link's target if it's safe to hand to the desktop, else None with the reason. Agents write these
    links, so only web pages and existing files or folders under LINK_ROOT (home, unless set) open, and
    nothing that would run (an executable, or a .desktop launcher)."""
    if target.startswith(("http://", "https://")):
        return target, None
    path = os.path.realpath(os.path.expanduser(target))
    if not path.startswith(LINK_ROOT + os.sep):
        return None, f"only links inside {LINK_ROOT.replace(os.path.expanduser('~'), '~', 1)} open"
    if not os.path.exists(path):
        return None, f"{os.path.basename(path)} doesn't exist (any more)"
    if path.endswith(".desktop") or (os.path.isfile(path) and os.access(path, os.X_OK)):
        return None, f"{os.path.basename(path)} is a program; open it yourself if you mean to"
    return path, None
