"""Screen state and data refresh; drawing and input live in the Renderer and Composer mixins. Owner: Claude."""

import curses
import functools
import os
import queue
import re
import threading
import textwrap
import time
from collections import deque

from agi import store, ui_state, updates
from agi.ui import composer
from agi.ui.composer import QUIT_WINDOW, Composer
from agi.ui import pixbuf, sound
from agi.ui.desktop import Desktop
from agi.ui.render import RAIL_WIDTH, Renderer
from agi.ui.text import ME, TASK, clean, fit


HISTORY = 1000              # messages kept in memory
STATUS_EVERY = 2            # seconds between presence/guard/latest-id refreshes (also the missed-wake bound)
NOTE_SECONDS = 20           # transient notes clear after this, or on the next key


class Writer(threading.Thread):
    """Runs disk writes (sends, draft saves) in order off the input thread; results return through a pipe."""

    def __init__(self, wake_fd):
        super().__init__(daemon=True)
        self.jobs, self.done, self.wake_fd = queue.Queue(), queue.Queue(), wake_fd

    def submit(self, kind, function, *args):
        self.jobs.put((kind, function, args))

    def run(self):
        while True:
            kind, function, args = self.jobs.get()
            try:
                self.done.put((kind, function(*args), None))
            except Exception as error:      # reported on screen by apply_writes
                self.done.put((kind, None, error))
            self.jobs.task_done()
            try:
                os.write(self.wake_fd, b"w")
            except BlockingIOError:
                pass                        # the main loop is already due to wake

    def drain(self, timeout):
        """Wait up to `timeout` seconds for queued writes (used when quitting). True if all finished."""
        deadline = time.monotonic() + timeout
        while self.jobs.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.jobs.unfinished_tasks


class Screen(Renderer, Composer):
    image_note_shown = frozenset()   # which missing-GTK notes appeared this run: "attach", "clipboard"

    def __init__(self, win):
        self.win = win
        self.messages = deque()
        self.by_id = {}
        self.tasks = {}             # user message id -> [(claim | release | done, agent, ts)] from task records
        self.task_list = None       # /tasks: store.list_tasks() from the status thread, over the whole log
        self.todo_list = None       # /todo: the open commitments (docs/Todo.md)
        self.todo_all = None        # every to-do, done ones too, so a clicked T number opens whatever it is
        self.board = None           # the notes /tasks or /todo put up: refreshed with each status pass while shown
        self.board_kind = "tasks"
        self.gate_board = None      # /gates notes, refreshed only while visible
        self.body_height = 20       # transcript rows at the last draw, for /find jumps
        self.hint_shown = False     # whether that draw had the scroll/history hint row
        self.pinned = None          # a message a jump put at the top, measured again at the next draw
        self.rows_top, self.shown_rows, self.row_owners = 0, [], []   # the drawn transcript, for clicks
        self.found = None           # (query, id) of the last /find match
        self.highlight = None       # the /find words shown highlighted, until Esc or a send
        self.view = None            # older messages shown by /find; None means the live transcript
        self.new_after = None       # the user's read marker at startup: a "new" rule goes after it
        self.marked = 0             # the read marker as last written
        self.seen = 0               # newest id the user has seen at the live bottom, waiting to be written
        self.seen_written_at = 0
        self.interacted = False     # set by real keys and focus-in; never by arrivals or focus-out
        self.last_key_at = time.monotonic()   # the status thread waits for a quiet moment before its cold scan
        self.drafts = None          # ui_state.DraftSession: the unsent draft survives crashes
        self.focused = True         # from the terminal's focus reports; notifications only when unfocused
        self.away = 0               # messages that arrived while unfocused, shown in the window title
        self.desktop = Desktop()    # popups coordinated with any other open screen of this chat
        self.title = "AGI"
        self.saved_state, self.edited_at = None, None
        self.writer = None          # Writer thread, set by the app; None means write in place (tests)
        self.sending = None         # draft snapshot of the send in flight; the draft stays until it commits
        self.typed_ahead = []       # keys pressed while it was in flight, replayed once it lands
        self.closing = False        # quit asked while a write was in flight: waiting for it, visibly
        self.force_quit = False     # quit anyway: the unsaved text is printed on exit
        self.unsaved = ""           # text that a forced quit left neither sent nor saved
        self.cache = {}             # message id -> rows, valid for self.cache_width
        self.cache_width = None
        self.last_id = 0
        self.guard = {"mode": None}         # filled by the status thread; the footer says "checking mode…" until then
        self.retry_deadline = None          # monotonic time a rate refusal ends, for the countdown
        self.refused = False        # a guard refusal happened since the last user message
        self.claims = {}
        self.presence = {}
        self.acks = {}              # participant -> the id they have handled through (status thread); {} until it arrives
        self.wakes = {}             # agent -> (first id, last id) ranges handed to it (wake receipts); {} until they arrive
        self.marks = {}             # message id -> ((participant, acked), ...): its read marks
        self.status_wanted = threading.Event()   # set to make the status thread refresh now
        self.draft, self.cursor = "", 0
        self.history, self.history_pos, self.stash = [], None, ""
        self.scroll = 0
        self.unseen = {}            # message id -> sender: arrived while scrolled up, not yet on screen
        self.notes = []             # transient rows: (text, attr_name, expires_at or None)
        self.whatsnew = self.release_board = None   # an unread release entry; /whatsnew's rows
        self.decision_board = None                  # /decisions' rows
        self.help = False
        self.asked = False          # the keys were opened by typing ? on an empty draft (the ? is in the draft)
        self.esc_at = self.ctrl_c_at = 0
        self.pending = []           # keys read but not yet handled (escape sequences)
        self.pending_tail = []      # keys fetched while finishing a split escape sequence
        self.mouse_events = deque()  # each KEY_MOUSE's details, read with it, in order
        self.menu_index = 0         # the selected row of the / command menu
        self.menu_closed = False    # Esc hides the menu until the next typed character
        self.pasting = None         # list of characters collected inside a bracketed paste
        self.paste_at = 0.0         # when its last character arrived
        self.chips = {}             # paste chip label -> the full pasted text
        self.chip_count = 0         # chip numbers never repeat within a session
        self.saved_draft = ("", {}, [])  # the draft in progress, its chips and images, while browsing history
        self.stash_chips = {}       # the chips of a draft cleared with Esc Esc
        self.images = []            # the draft's attached images (store references), in order
        self.stash_images = []      # the images of a draft cleared with Esc Esc
        self.attaching = 0          # images being stored by the writer thread
        self.chip_numbers = {}      # a chip moved to a new character keeps the number its label shows
        self.drawn_minute = 0       # activity durations redraw once a minute
        self.running = True
        self._colors()

    def _colors(self):
        A = curses
        self.attr = {"user": A.A_BOLD, "claude": A.A_BOLD, "codex": A.A_BOLD, "dim": A.A_DIM,
                     "error": A.A_BOLD, "admin": A.A_BOLD, "meta": A.A_DIM, "me": A.A_REVERSE | A.A_BOLD,
                     "band": 0, "rule": A.A_DIM, "key": A.A_BOLD, "code": A.A_BOLD, "menu_on": A.A_REVERSE,
                     "found": A.A_REVERSE, "sending": A.A_DIM, "quote": A.A_DIM}
        self.on_band = {}                   # colour pair -> the same colour on your messages' band
        if os.environ.get("NO_COLOR") or not curses.has_colors():
            return
        curses.use_default_colors()
        rich = curses.COLORS >= 256
        light = os.environ.get("AGI_THEME") == "light"   # set by app.detect_theme() unless the user chose
        palette = {"user": 28 if rich and light else curses.COLOR_GREEN,
                   "claude": (166 if light else 173) if rich else curses.COLOR_YELLOW,
                   # The brand's blue is #6a86f2 on dark and #1f5ae6 on light, approximated in 256 colours.
                   "codex": (26 if light else 69) if rich else curses.COLOR_BLUE,
                   "error": curses.COLOR_RED,
                   # the admin's muted amber; with few colours cyan, which the other names don't use
                   "admin": (136 if light else 178) if rich else curses.COLOR_CYAN}
        for number, (name, color) in enumerate(palette.items(), 1):
            curses.init_pair(number, color, -1)
            self.attr[name] |= curses.color_pair(number)
        curses.init_pair(10, (25 if light else 110) if rich else curses.COLOR_CYAN, -1)
        if rich:                            # /find matches: dark text on yellow, readable in both themes
            curses.init_pair(11, 16, 220)
            self.attr["found"] = curses.color_pair(11)
        self.attr["code"] = curses.color_pair(10)
        if rich:                            # a faint band behind your own messages, in either theme
            band = 254 if light else 236
            curses.init_pair(8, -1, band)
            self.attr["band"] = curses.color_pair(8)
            curses.init_pair(9, palette["user"], band)
            self.attr["user_band"] = curses.color_pair(9) | curses.A_BOLD
        # @claude / @codex in text: the agents' colours, paler than their names in headers but easy to read
        soft = {"claude": (137 if light else 180) if rich else curses.COLOR_YELLOW,
                "codex": (26 if light else 69) if rich else curses.COLOR_BLUE,   # @codex: its blue, not bold
                "admin": (94 if light else 143) if rich else curses.COLOR_CYAN}
        for number, (name, color) in enumerate(soft.items(), 12):
            curses.init_pair(number, color, -1)
            self.attr[f"{name}_mention"] = curses.color_pair(number)
        if rich and curses.COLOR_PAIRS > 40:   # every coloured style on the band keeps the band behind it: a pair
            for pair in (1, 2, 3, 4, 5, 10, 12, 13, 14):   # with the default background punched a dark hole there
                curses.init_pair(20 + pair, curses.pair_content(pair)[0], band)
                self.on_band[pair] = 20 + pair

    # -- data -------------------------------------------------------------

    def refresh_data(self, initial=False):
        if initial and hasattr(store, "read_tail"):
            new = store.read_tail(HISTORY)  # only the newest messages: fast first paint on long logs
        else:
            new = store.read_messages(self.last_id)
        ring, notify, mentioned = None, [], None   # the message that rang, and why
        # reading older messages: what's on screen stays put as new ones arrive below it (UI bug hunt,
        # 2026-11-18). Anchored on the message at the bottom of the view, not a row count: at the history
        # limit an arrival also drops rows at the top, and the count came out even (Codex, #1447)
        anchor = self._view_anchor() if not initial and new and self.scroll and self.view is None else None
        for message in new:
            self._add(message)
            if not initial and message["sender"] != store.USER:
                if self.scroll or self.view is not None:
                    self.unseen[message["id"]] = message["sender"]
                addressed = self.addressed_to_me(message)
                if addressed or self._is_refusal(message):
                    ring = (message, self.why_addressed(message) or "a guard refused a post")
                if addressed:
                    mentioned = ring
                    if not self.focused:
                        notify.append(message)
                if not self.focused and message["kind"] == "msg":
                    self.away += 1
        if new:
            self.last_id = new[-1]["id"]
            self.update_marks()
        if anchor is not None:
            self._restore_anchor(anchor)
        if not initial:                     # at startup the first draw comes first; status follows
            self.request_status()
        if ring:
            curses.beep()
            sound.log("terminal bell", *ring)
        if notify:
            self.desktop_notify(notify)    # with its sound, unless /sound off
        elif mentioned and self.focused and sound.mode() == "always":
            sound.log("sound (always)", *mentioned)
            sound.play()

    def _view_anchor(self):
        """(message id, its rows below the view's bottom edge) for the message at the bottom of the view; only
        the blocks below the view are measured."""
        below = 0
        for message, block in self.blocks_backward(self.cache_width or 80):
            if below + len(block) > self.scroll:
                return message["id"], self.scroll - below
            below += len(block)
        return None

    def _restore_anchor(self, anchor):
        message_id, inside = anchor
        below = 0
        for message, block in self.blocks_backward(self.cache_width or 80):
            if message["id"] == message_id:
                self.scroll = below + inside
                return
            below += len(block)

    def _add(self, message):
        if len(self.messages) >= HISTORY:
            old = self.messages.popleft()
            self.by_id.pop(old["id"], None)
            self.cache.pop(old["id"], None)
        self.messages.append(message)
        self.by_id[message["id"]] = message
        kind, sender, text = message["kind"], message["sender"], message["text"]
        if kind == "msg" and sender == store.USER:
            self.refused = False
            self.history.append(text)
        elif self._is_refusal(message):
            self.refused = True
        task = TASK.match(text) if kind in ("claim", "release") else None
        if task and kind == "claim" and message.get("doing_update"):   # what the owner is doing changed, not who owns
            task = None                     # it: the line under the request keeps who first took it (Codex, #5231)
        if task:
            target = int(task.group(1))
            ended = "done" if kind == "release" and message.get("done") is True else kind   # a release can be a
            self.tasks.setdefault(target, []).append((ended, sender, message["ts"]))           # handoff: only "done" is done
            self.cache.pop(target, None)

    @staticmethod
    def _is_refusal(message):
        return message["sender"] == "system" and message["text"].startswith(("Agent loop guard", "Agent rate guard"))

    def why_addressed(self, message):
        """Why a message is for the person, one rule for the sound log and the notification: "mentions you" (an
        @mention), "answers your message" (↪ #N is one of theirs; store.reply_target through addressed_to), else None."""
        if self.mentions_me(message):
            return "mentions you"
        return "answers your message" if self.addressed_to_me(message) else None

    def image_support_missing(self, kind):
        """An image paste or drop failed only because GTK for Python isn't here: say so once per run."""
        if kind == "attach":
            self.attaching = max(0, self.attaching - 1)
        else:
            self.notes = [note for note in self.notes if note[0] != "Reading the clipboard…"]
        if kind not in self.image_note_shown:   # each note once per run
            self.image_note_shown = self.image_note_shown | {kind}
            self.note(pixbuf.MISSING_NOTE if kind == "attach" else pixbuf.CLIPBOARD_NOTE, "error")

    def desktop_notify(self, messages):
        """A desktop notification for mentions while the window is unfocused. AGI_NOTIFY=0 turns it off."""
        if not self.notify_on:              # turned off in /settings
            return
        latest = messages[-1]
        verb = "replied to" if self.why_addressed(latest) == "answers your message" else "mentioned"
        title = f"{latest['sender']} {verb} you" + (f" (+{len(messages) - 1} more)" if len(messages) > 1 else "")
        self.desktop.notify(title, fit(" ".join(clean(latest["text"]).split()), 200), latest)

    # -- crash-safe state: read marker and draft ----------------------------

    def open_state(self):
        try:                                # T4270: new in this version, once, until /whatsnew or Esc (T4177's receipt)
            quiet = os.environ.get("AGI_RELEASE_NOTICE") == "0"     # tests: only the tests about it see it
            self.whatsnew = None if quiet else updates.unread_release(store.USER)
        except (ValueError, OSError):       # notes we can't read are no reason to stop the screen
            self.whatsnew = None
        try:
            marker = ui_state.get_read_marker()
            self.marked = self.seen = marker
            if self.messages and self.messages[0]["id"] <= marker < self.last_id:
                self.new_after = marker
        except (ValueError, OSError) as error:
            self.note(f"Read marker unavailable: {error}", "error")
        try:
            self.drafts = ui_state.DraftSession()
        except (ValueError, OSError) as error:
            self.note(f"Draft saving unavailable: {error}", "error")
            return
        for warning in self.drafts.warnings:
            self.note(warning, "error", sticky=True)
        restored = self.drafts.restored
        if restored:
            self.draft, self.cursor = restored["draft"], restored["cursor"]
            self.chips, self.chip_count = dict(restored["chips"]), restored["chip_count"]
            self.images = [dict(ref) for ref in restored.get("attachments", [])]
            self.note("Restored your unsent draft")
        self.saved_state = self._draft_state()

    def _draft_state(self):
        return (self.draft, self.cursor, dict(self.chips), self.chip_count, [dict(ref) for ref in self.images])

    def save_draft(self, now=False):
        """Save the draft about a second after the last edit, or right away with now=True."""
        if not self.drafts or self.sending:
            return                          # while sending, the send's journal holds this draft; a save
        state = self._draft_state()         # queued behind it would bring the sent text back as a ghost
        if state == self.saved_state:
            self.edited_at = None
            return
        if self.edited_at is None:
            self.edited_at = time.monotonic()
        if not now and time.monotonic() - self.edited_at < 1:
            return
        if self.writer:                     # off the input thread; a failure comes back via apply_writes
            self.writer.submit("save", functools.partial(self.drafts.save, attachments=state[4]), *state[:4])
            self.saved_state, self.edited_at = state, None
            return
        try:
            self.drafts.save(*state[:4], attachments=state[4])
            self.saved_state, self.edited_at = state, None
        except (ValueError, OSError) as error:
            self.note(f"Draft not saved: {error}", "error")
            self.edited_at = time.monotonic()

    def apply_writes(self):
        """Take finished writes from the writer thread. True if the screen changed."""
        changed = False
        while self.writer and not self.writer.done.empty():
            kind, result, error = self.writer.done.get()
            changed = True
            if kind in ("seen", "position"):   # read marker and reading position are convenience saves
                continue
            if kind == "todo-next":
                self.next_todo_result(result, error)
                continue
            if kind == "todo-check":
                self.note(f"Not saved · {error}" if error else result, "error" if error else "dim")
                continue
            if isinstance(error, pixbuf.ImageSupportMissing) and kind in ("attach", "clipboard"):
                self.image_support_missing(kind)    # one note per run, not the paste's own error
                continue
            if kind == "attach":            # an image stored: it joins the draft (or says why not)
                self.attached(result, error)
                continue
            if kind == "copy":              # the selection is on the clipboard (or why not)
                self.copied(result, error)
                continue
            if kind == "clipboard":         # Ctrl+V: the clipboard's text or image
                self.pasted(result, error)
                continue
            if kind == "save":
                if error:
                    self.saved_state = None     # try again on the next save
                    reason = error.strerror.lower() if isinstance(error, OSError) and error.strerror else str(error)
                    if self.closing:            # don't quit with unsaved text: stay open and say why
                        self.closing = False
                        self.note(f"Draft not saved: {reason} · your text is still here; Ctrl+C twice to retry",
                                  "error", sticky=True)
                    else:
                        self.note(f"Draft not saved: {reason}", "error")
                continue
            snapshot, self.sending = self.sending, None
            if error:
                reason = error.strerror.lower() if isinstance(error, OSError) and error.strerror else str(error)
                self.note(f"Not sent: {reason} · your text is still here, press Enter to retry", "error", sticky=True)
                self._keep_typed_ahead()
                continue
            self.draft, self.cursor, self.history_pos, self.stash, self.chips = "", 0, None, "", {}
            self.stash_chips, self.images = {}, []
            self.saved_state = self._draft_state()  # the send cleared the saved draft; your place in the chat stays
            self.notes = [n for n in self.notes if n[1] != "error"]
            try:
                self.refresh_data()
            except (ValueError, OSError) as error:
                self.note(f"Sent · the screen couldn't refresh yet: {error}", "error", sticky=True)
            self._replay_typed_ahead()
        return changed

    def _keep_typed_ahead(self):
        """After a failed send, what was typed meanwhile joins the draft as text, an Enter as a new line, so
        nothing sends by itself: retrying is a deliberate Enter (Codex, #1431)."""
        ahead, self.typed_ahead = self.typed_ahead, []
        text = "".join("\n" if key in ("\r", "\n") else key for key in ahead
                       if key in ("\r", "\n") or key.isprintable())
        if text.strip():
            self.draft = self.draft.rstrip("\n") + "\n" + text.lstrip("\n")
            self.cursor = len(self.draft)

    def _replay_typed_ahead(self):
        """The keys pressed during the send, now that the draft is free; an Enter among them sends again and
        the rest waits for that send in turn."""
        ahead, self.typed_ahead = self.typed_ahead, []
        for index, key in enumerate(ahead):
            if self.sending:
                self.typed_ahead = ahead[index:] + self.typed_ahead
                return
            self.key(key)

    def capture_seen(self):
        """After an interaction at the live bottom, everything on screen counts as seen."""
        # typing a /command isn't reading the chat (#6113: /catchup at the bottom marked 38 messages read)
        if self.interacted and self.view is None and not self.scroll and not self.draft.startswith("/"):
            self.seen = max(self.seen, self.last_id)
        self.interacted = False

    def flush_seen(self, now=False):
        """Write the captured id off the input thread, at most every 2 s; now=True writes it here (exit)."""
        if self.seen <= self.marked or (not now and time.monotonic() - self.seen_written_at < 2):
            return
        self.seen_written_at, target = time.monotonic(), self.seen

        def write():
            try:
                self.marked = max(self.marked, ui_state.mark_read(target))
            except (ValueError, OSError):
                pass                        # the marker is a convenience; never interrupt typing
        if now:
            write()
        else:
            threading.Thread(target=write, daemon=True).start()

    def writes_pending(self):
        return bool(self.writer and (self.sending or self.writer.jobs.unfinished_tasks))

    def queue_final_writes(self):
        """On a normal quit: queue the dirty draft and the read marker on the writer, then wait for them."""
        if not self.writer:
            return
        self.save_draft(now=True)           # queues; a no-op when the draft is already saved
        if self.seen > self.marked:
            self.writer.submit("seen", ui_state.mark_read, self.seen)
            self.marked = self.seen

    def close_state(self):
        if self.writer:
            finished = self.writer.drain(0 if self.force_quit else 15)
            self.apply_writes()
            self.writer = None
            dirty = bool(self.draft or self.images) and self._draft_state() != self.saved_state
            if self.force_quit or not finished:
                # Forced or signal exit, or a stuck disk: no writes from here (they could block on the same
                # disk) and no assumption that a journal landed. Print anything not known to be saved.
                if self.sending or dirty or not finished:
                    draft, _, chips, *_ = self.sending or self._draft_state()
                    self.chips = chips      # print what the user typed and pasted, not chip labels
                    self.unsaved = self.expand(draft)
                return
            if self.drafts:                 # everything was written by the writer; just release the lock
                self.drafts.close()
            return
        self.save_draft(now=True)
        self.flush_seen(now=True)          # only what an interaction captured, never later arrivals
        if self.drafts:
            self.drafts.close()

    def request_status(self):
        """Ask the status thread for fresh guard/claims/presence; the store reads never block input."""
        self.status_wanted.set()

    def apply_status(self, result):
        """Take (guard, claims, presence, latest id, tasks, to-dos, acks, wakes) or an error from the status thread.
        True if it changed."""
        if isinstance(result, Exception):
            self.note(f"Store error: {result}", "error")
            return True
        guard, claims, presence, latest, tasks, todos, acks, wakes = result
        before = (self.guard, self.claims, self.presence, self.task_list, self.todo_all, self.acks, self.wakes)
        self.set_guard(guard)
        self.claims, self.presence, self.task_list, self.todo_all = claims, presence, tasks, todos
        self.todo_list = None if todos is None else [todo for todo in todos if todo["result"] is None]
        self.acks, self.wakes = acks, wakes
        changed = (guard, claims, presence, tasks, todos, acks, wakes) != before
        if (acks, wakes) != before[5:]:     # read marks move
            self.update_marks()
        if changed and self.board and self.notes == self.board:   # still showing a board (by value: the notes
            self.show_board(self.board_kind)                      # list is rebuilt on every draw): keep it current
        if latest > self.last_id:           # a wake was missed: catch up now instead of on the next key
            try:
                self.refresh_data()
            except (ValueError, OSError) as error:   # a bad log line: say so, keep the screen and the draft
                self.note(f"Store error: {error}", "error")
            changed = True
        return changed

    def gates_visible(self):
        return self.notes == getattr(self, "gate_board", None)

    def show_release(self):
        """/whatsnew: the installed version's changelog entry, wrapped to the screen, first lines first; reading it
        is the receipt, as Esc on the notice is."""
        release = updates.current_release()
        if release is None:
            self.note("No release notes for this version.")
            return
        width = max(20, self.win.getmaxyx()[1] - (RAIL_WIDTH + 1 if self.rail_shown else 0) - 4)
        rows = [(f"AGI {release['version']} · {release['date']} · Esc closes", "key")]
        items = []                          # the changelog wraps its own lines: each bullet joined back first
        for line in release["markdown"].splitlines():
            bare = line.strip()
            if items and bare and items[-1] and not bare.startswith(("- ", "* ", "#")):
                items[-1] += " " + bare
            else:
                items.append(bare)
        for item in items:
            if item.startswith("#"):        # a heading ("### Added") as a plain, bold word
                rows.append((item.lstrip("# "), "key"))
                continue
            rows += [(text, "dim") for text in textwrap.wrap(item, width, subsequent_indent="  ")] or [("", "dim")]
        self.notes = self.release_board = [(text, kind, composer.UNTIL_KEY) for text, kind in rows]
        self.acknowledge_release(release["version"])

    def acknowledge_release(self, version=None):
        version = version or (self.whatsnew or {}).get("version")
        self.whatsnew = None
        if version:
            try:
                updates.acknowledge_release(store.USER, version)
            except (ValueError, OSError) as error:
                self.note(f"Couldn't record that you've read it: {error}", "error")

    def show_decisions(self):
        """Show marked questions, their selections and links to the original answers."""
        try:
            records = store.list_decisions()
        except (OSError, ValueError) as error:
            self.note(f"Decisions unavailable: {error}", "error")
            return
        if not records:
            self.note("No marked questions yet: an agent marks one with its choices (1a, 1b, …).")
            return
        rows = [("Decisions · newest first · ✎ you added words · click a # to read them · Esc closes", "key")]
        for record in sorted(records, key=lambda record: -record["question_id"]):
            picks = sorted(record["selections"].items(), key=lambda pick: int(pick[0]))
            chosen = " ".join(pick["choice"] + ("✎" if pick["qualified"] else "") for _, pick in picks)
            asked = sorted({code[:-1] for code in record["choices"]}, key=int)
            open_ = [number for number in asked if number not in record["selections"]]
            where = sorted({pick["answer_id"] for _, pick in picks})
            line = f"#{record['question_id']} {record['project']} · {chosen or 'no answer yet'}"
            if where:
                line += " · in " + " ".join(f"#{answer}" for answer in where)
            if record["review_answer_id"]:
                line += f" · needs a look: #{record['review_answer_id']}"
            elif open_ and chosen:
                line += " · open: " + " ".join(open_)
            rows.append((line, "dim"))
            question = self.by_id.get(record["question_id"])
            if question:
                plain = re.sub(r"\*\*|`", "", " ".join(clean(question["text"]).split()))
                rows.append(("  " + plain[:200], "dim"))
        self.notes = self.decision_board = [(text, kind, composer.UNTIL_KEY) for text, kind in rows]

    def show_found(self, query, before=None, below=False):
        """/find (#6054): the matches as a list, each a click away (Tab returns to where you were). `below`: under
        the note of the jump /find just made, listing the older ones after it. Looking leaves the read marker and the
        reading position alone (#6079)."""
        from agi import history_view
        try:
            result = history_view.find(query, before=before, limit=8)
        except (OSError, ValueError) as error:
            if not below:
                self.note(str(error), "error")
            return
        hits = result["messages"]
        if not hits:
            if not below:
                self.note(f"No messages match {query!r}", "error")
            return
        if not below:
            self.keep_place()               # a from: list: Tab comes back here from its hits too (#6103)
        if below:
            rows = [(f"Older matches{' (more with /find)' if result['more'] else ''} · click a # to jump, Tab "
                     "returns", "key")]
        else:
            rows = [(f"Found {len(hits)}{'+' if result['more'] else ''} for {query!r} · newest first · click a # "
                     "to jump, Tab returns · Esc closes", "key")]
        for hit in hits:
            when = hit["ts"][11:16]
            plain = re.sub(r"\*\*|`", "", " ".join(clean(hit["text"]).split()))
            rows.append((f"#{hit['id']} {hit['sender']} {when} · {plain[:160]}", "dim"))
        found = [(text, kind, composer.UNTIL_KEY) for text, kind in rows]
        above = [(text, kind, composer.UNTIL_KEY) for text, kind, _ in self.notes] if below else []
        self.notes = above + found
        self.start_pick(len(above))

    def show_catchup(self):
        """/catchup (#6052, #6054): what is provably waiting for you since your read marker, what finished and
        what's in hand, built from records, never a summary; everything else stays unread."""
        from agi import history_view
        after = self.new_after if self.new_after is not None else self.marked
        try:
            view = history_view.catch_up(after or 0)
        except (OSError, ValueError) as error:
            self.note(f"Catch-up unavailable: {error}", "error")
            return
        link = {"decision": "#", "message": "#", "task": "#", "todo": "T"}
        rows = [("Since you last read · click a # or T to open it · Tab returns · Esc closes", "key")]
        for title, key, kind in (("Waiting for you", "waiting", "user"), ("Finished", "finished", "dim"),
                                 ("In hand", "active", "dim")):
            items = view[key]
            if not items:
                continue
            rows.append((f"{title} ({len(items)})", kind))
            for item in items[:8]:
                plain = re.sub(r"\*\*|`", "", " ".join(clean(item["text"]).split()))
                rows.append((f"  {link.get(item['kind'], '#')}{item['id']} · {plain[:150]}", "dim"))
            if len(items) > 8:
                rows.append((f"  and {len(items) - 8} more", "dim"))
        if len(rows) == 1:
            rows.append(("Nothing is waiting for you.", "dim"))
        rows.append((f"{view['other_unread']} other unread message{'s' if view['other_unread'] != 1 else ''} stay "
                     "unread below the new line.", "dim"))
        self.notes = [(text, kind, composer.UNTIL_KEY) for text, kind in rows]
        self.keep_place()
        self.start_pick()

    def show_gates(self):
        self.notes = self.gate_board = [("Checking integration gates…", "dim", composer.UNTIL_KEY)]
        self.request_status()

    def apply_gates(self, snapshot):
        rail = self.rail_shown and snapshot != self.gate_snapshot
        self.gate_snapshot = snapshot
        if not self.gates_visible():
            return rail
        rows = []
        for project, stage, owner, queued in snapshot or ():
            line = " · ".join((project, stage, *filter(None, owner)))
            if queued:
                names = ", ".join(queued[:2])
                extra = f" +{len(queued) - 2}" if len(queued) > 2 else ""
                line += f" · waiting {names}{extra}"
            rows.append((line, "dim", composer.UNTIL_KEY))
        if not rows:
            rows.append(("Integration status unavailable", "dim", composer.UNTIL_KEY))
        changed = rows != self.gate_board
        self.notes = self.gate_board = rows
        return changed

    def set_guard(self, guard):
        """Store a guard status. A blocked rate mode with a positive retry gets a deadline to count down to;
        paused and wait have no retry time, so they never get one (that would refresh in a loop)."""
        self.guard = guard
        retry = guard.get("retry_after")
        rate = guard.get("mode") in ("normal", "custom", "auto", "autonomous")
        timed = guard.get("blocked") and rate and isinstance(retry, (int, float)) and retry > 0
        self.retry_deadline = time.monotonic() + retry if timed else None

    def retry_left(self):
        deadline = getattr(self, "retry_deadline", None)
        return max(0, deadline - time.monotonic()) if deadline else 0

    def ticking(self):
        """True while something on screen changes by the second: a working agent's elapsed time, a rate
        countdown, or a two-press hint ('ctrl+c again to quit', 'esc again to clear') about to expire."""
        working = any(info.get("state") == "working" and info.get("since")
                      and time.time() - info["since"] < 3600 for info in self.presence.values())
        hint = time.time() - max(self.ctrl_c_at, self.esc_at) < QUIT_WINDOW + 1
        return working or self.retry_left() > 0 or hint

    def note(self, text, kind="dim", sticky=False, until_key=False):
        """One note: it clears after NOTE_SECONDS or on the next key; `until_key` keeps it until that key however
        long it takes; `sticky` keeps it until another note replaces it (a failed send, an export's name)."""
        expires = None if sticky else composer.UNTIL_KEY if until_key else time.monotonic() + NOTE_SECONDS
        self.notes = [(text, kind, expires)]
