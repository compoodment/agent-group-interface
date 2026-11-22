"""Chat screen tests: run the real bin/agi in a pseudo-terminal and read back what it drew.

Owner: Claude. Stdlib only. Every test uses its own throwaway data folder, never the live chat.
Run: python3 -I tests/test_ui.py
"""

import codecs
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import statistics
import struct
import subprocess
import sys
import collections
import tempfile
import termios
import time
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # once, for every test: run under -I and split into shards, a test that imported agi
                                # only worked after another had put this here (ChipTests, #5054)
LAUNCHER = str(ROOT / "bin" / "agi")
SCRATCH = os.environ.get("AGI_SCRATCH")


def _cells(ch):
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


class Terminal:
    """Just enough of xterm to turn ncurses output back into text."""

    def __init__(self, rows, cols):
        self.resize(rows, cols)
        self.cur, self.saved, self.buf, self.last = (0, 0), (0, 0), "", " "

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.top, self.bottom = 0, rows - 1
        clamp = lambda pos: (min(pos[0], rows - 1), min(pos[1], cols - 1))
        self.cur, self.saved = clamp(getattr(self, "cur", (0, 0))), clamp(getattr(self, "saved", (0, 0)))

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.grid)

    def _put(self, ch):
        row, col = self.cur
        row, col = min(row, self.rows - 1), min(col, self.cols - 1)
        width = _cells(ch)
        if not width:
            return
        self.grid[row][col] = ch
        if width == 2 and col + 1 < self.cols:
            self.grid[row][col + 1] = ""
        self.last, self.cur = ch, (row, min(col + width, self.cols))

    def _scroll(self, up=True):
        if up:
            del self.grid[self.top]
            self.grid.insert(self.bottom, [" "] * self.cols)
        else:
            del self.grid[self.bottom]
            self.grid.insert(self.top, [" "] * self.cols)

    def feed(self, data):
        data = self.buf + data
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                match = re.match(r"\x1b\[([?>=]?)([\d;]*)([@-~])", data[i:])
                if match:
                    if not match.group(1):
                        self._csi(match.group(2), match.group(3))
                    i += match.end()
                    continue
                match = re.match(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()]\w|\x1b[=>78DEMc]", data[i:])
                if match:
                    code = match.group(0)
                    if code == "\x1b7":
                        self.saved = self.cur
                    elif code == "\x1b8":
                        self.cur = self.saved
                    elif code == "\x1bM":
                        if self.cur[0] == self.top:
                            self._scroll(False)
                        else:
                            self.cur = (self.cur[0] - 1, self.cur[1])
                    i += match.end()
                    continue
                if i + 1 >= len(data) or len(data) - i < 16:
                    break                   # an incomplete sequence: wait for more
                i += 2
                continue
            if ch == "\r":
                self.cur = (self.cur[0], 0)
            elif ch == "\n":
                if self.cur[0] == self.bottom:
                    self._scroll()
                else:
                    self.cur = (self.cur[0] + 1, self.cur[1])
            elif ch == "\b":
                self.cur = (self.cur[0], max(0, self.cur[1] - 1))
            elif ch not in "\x07\x0e\x0f":
                self._put(ch)
            i += 1
        self.buf = data[i:]

    def _csi(self, args, final):
        self._csi_raw(args, final)
        # A real terminal clamps cursor moves to the screen, e.g. output drawn just before a resize.
        self.cur = (max(0, min(self.cur[0], self.rows - 1)), max(0, min(self.cur[1], self.cols)))

    def _csi_raw(self, args, final):
        values = [int(x) if x else 0 for x in args.split(";")] if args else []
        n = values[0] if values and values[0] else 1
        row, col = self.cur
        blank = [" "] * self.cols
        if final in "Hf":
            self.cur = ((values[0] or 1) - 1 if values else 0, (values[1] or 1) - 1 if len(values) > 1 else 0)
        elif final == "A":
            self.cur = (max(0, row - n), col)
        elif final == "B":
            self.cur = (min(self.rows - 1, row + n), col)
        elif final == "C":
            self.cur = (row, min(self.cols - 1, col + n))
        elif final == "D":
            self.cur = (row, max(0, col - n))
        elif final == "G":
            self.cur = (row, n - 1)
        elif final == "d":
            self.cur = (n - 1, col)
        elif final == "J":
            mode = values[0] if values else 0
            if mode == 0:
                self.grid[row][col:] = blank[col:]
                lines = range(row + 1, self.rows)
            else:
                lines = range(0, row) if mode == 1 else range(self.rows)
            for y in lines:
                self.grid[y] = list(blank)
        elif final == "K":
            mode = values[0] if values else 0
            if mode == 0:
                self.grid[row][col:] = blank[col:]
            elif mode == 1:
                self.grid[row][:col + 1] = blank[:col + 1]
            else:
                self.grid[row] = list(blank)
        elif final == "X":
            self.grid[row][col:col + n] = blank[:min(n, self.cols - col)]
        elif final == "b":
            for _ in range(n):
                self._put(self.last)
        elif final == "r":
            # clamped to the screen, as a terminal does: a frame drawn for the size before a resize can name more rows
            self.top = min((values[0] or 1) - 1 if values else 0, self.rows - 1)
            self.bottom = min((values[1] or self.rows) - 1 if len(values) > 1 else self.rows - 1, self.rows - 1)
            self.cur = (0, 0)
        elif final in "LM":
            for _ in range(n):
                if final == "L":
                    del self.grid[self.bottom]
                    self.grid.insert(row, list(blank))
                else:
                    del self.grid[row]
                    self.grid.insert(self.bottom, list(blank))
        elif final in "ST":
            for _ in range(n):
                self._scroll(final == "S")


class ChatScreen:
    """bin/agi running in a pty with its own data folder."""

    def __init__(self, data, rows=24, cols=80, background=None, **env):
        self.data = data
        # never a real sound or a new-version line from a test: a test that checks one asks for it
        self.env = dict(os.environ, AGI_DATA=data, TERM="xterm-256color", **{"AGI_SOUND_CMD": "agi-test-no-sound", "AGI_RELEASE_NOTICE": "0", "AGI_SETUP_NOTICE": "0", "AGI_GATES": "0", **env})
        if background is None:              # the test pty can't answer the colour query: don't wait for it
            self.env.setdefault("AGI_THEME", "dark")
        else:
            self.env.pop("AGI_THEME", None)
        self.env.pop("NO_COLOR", None)
        self.term = Terminal(rows, cols)
        self.raw = b""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")   # a character may span reads
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.execve(LAUNCHER, [LAUNCHER], self.env)
        if background:                      # answer the colour query the way a terminal would
            self.until(lambda: b"\x1b]11;?" in self.raw, 10)
            os.write(self.fd, f"\x1b]11;rgb:{background}\x07".encode())
        self.pump_until("❯", 20)

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.01)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    return
                self.raw += chunk
                self.term.feed(self.decoder.decode(chunk))

    def pump_until(self, text, seconds=3):
        end = time.time() + seconds
        while text not in self.term.text() and time.time() < end:
            self.pump(0.05)
        return text in self.term.text()

    def type(self, data, settle=0.05):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)
        self.pump(settle)

    def until(self, condition, seconds=15):
        """Pump output until condition() holds; return whether it did. Replaces fixed sleeps.

        Generous by design: a passing check returns at once, but in the gate the store suite runs in
        parallel and its fsyncs can delay a large journalled send on the USB drive by several seconds.
        """
        end = time.time() + seconds
        while time.time() < end:
            try:
                if condition():
                    return True
            except (IndexError, KeyError, ValueError, OSError):
                pass
            self.pump(0.02)
        return condition()

    def resize(self, rows, cols):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.term.resize(rows, cols)
        os.kill(self.pid, signal.SIGWINCH)
        self.until(lambda: "─" * min(cols, 20) in self.term.text() and "❯" in self.term.text())
        self.pump(0.1)

    def chat(self, *args):
        """Run the CLI while still reading the screen's terminal. Waiting without reading deadlocked: the screen
        blocked writing a frame into the full terminal while holding Python's lock, its status thread held the
        store's lock waiting for Python's, and the CLI waited for the store's (test 78 hung on the Mini)."""
        process = subprocess.Popen([LAUNCHER, *args], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        while process.poll() is None:
            self.pump(0.02)
        stdout, stderr = process.communicate()
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        return json.loads(stdout) if stdout.strip().startswith("{") else result

    def messages(self):
        folder = Path(self.data) / "chat"
        return [json.loads(line) for path in sorted(folder.glob("*.jsonl"))
                for line in path.read_text().splitlines()]

    def quit_wait(self, seconds=10):
        """Wait for the process to exit on its own; return its exit code (None if it didn't)."""
        end = time.time() + seconds
        while time.time() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.pump(0.05)
                return os.waitstatus_to_exitcode(status)
            self.pump(0.02)
        return None

    def quit(self):
        self.type("\x03\x03", 0)
        end = time.time() + 5
        while time.time() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.pump(0.05)
                return os.waitstatus_to_exitcode(status)
            self.pump(0.02)
        return None


JOURNAL_SHIM = """
import os, time
from agi import store
_atomic_json = store._atomic_json

def _journal(path, value):
    # Only the pending-send journal write, where the real stall happened.
    if isinstance(value, dict) and "pending_send" in value:
        while os.path.exists(os.environ["AGI_TEST_GATE"]):
            time.sleep(0.05)
        if os.path.exists(os.environ["AGI_TEST_FAIL"]):
            raise OSError(5, "injected journal failure")
    elif isinstance(value, dict) and "draft" in value:      # an ordinary draft save
        if os.path.exists(os.environ["AGI_TEST_FAIL"] + "-saves"):
            raise OSError(5, "injected save failure")
    return _atomic_json(path, value)

store._atomic_json = _journal
"""


def seed(data, count):
    """Write `count` plain messages straight into a throwaway log (fast, no fsync per line), on a fixed day long
    past: on today's date, agent posts stamped a minute ago counted against the posting window, and a run near
    09:MM changed the footer's "4/5 per min" mid-test (Codex, #5694, #5695)."""
    folder = Path(data) / "chat"
    folder.mkdir(parents=True)
    day = "2026-01-05"
    words = "paste store cache wrap screen claim task review café 日本語 🙂 merged tests pass".split()
    with (folder / f"{day}.jsonl").open("w") as log:
        for number in range(1, count + 1):
            sender = ("user", "claude", "codex")[number % 3]
            text = " ".join(words[(number * k) % len(words)] for k in range(1, 4 + number % 30))
            log.write(json.dumps({"id": number, "ts": f"{day}T09:{number % 60:02d}:00+01:00",
                                  "sender": sender, "kind": "msg", "text": text}) + "\n")
    (Path(data) / "seq").write_text(f"{count}\n")


def admin_ack(data, message_id):
    """Supply an admin read marker from a saved conversation to the screen fixture."""
    cursor = Path(data) / "admin-mentions" / "cursor.json"
    cursor.parent.mkdir(exist_ok=True)
    pending = cursor.with_suffix(".pending")
    pending.write_text(json.dumps({"acked": message_id}), encoding="utf-8")
    pending.replace(cursor)


def woke(data, agent, last, first=None):
    """A delivery receipt in a throwaway data folder, as the channel and the bridge record one."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from agi import store; "
            "store.woke(sys.argv[2], int(sys.argv[3]), first_id=int(sys.argv[4]))")
    subprocess.run([sys.executable, "-c", code, str(ROOT), agent, str(last), str(first or last)],
                   env=dict(os.environ, AGI_DATA=data), check=True)


class SubmitTests(unittest.TestCase):
    """Screen.submit without a terminal: a refresh failure must not look like a failed send."""

    def test_refresh_failure_after_a_successful_send_clears_the_draft(self):
        import sys
        from unittest import mock
        sys.path.insert(0, str(ROOT))
        from agi import store, ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        screen.draft, screen.cursor, screen.history, screen.notes = "hello", 5, [], []
        screen.history_pos, screen.stash, screen.scroll, screen.unseen = None, "", 0, {}
        screen.chips, screen.chip_count = {}, 0
        screen.drafts, screen.view = None, None
        sent = []
        with mock.patch.object(store, "append_message", lambda *a: sent.append(a)), \
                mock.patch.object(screen, "refresh_data", side_effect=OSError(5, "Input/output error"), create=True):
            screen.submit("hello")
        self.assertEqual(len(sent), 1)
        self.assertEqual(screen.draft, "")
        self.assertIn("Sent", screen.notes[0][0])


class LinkHintTests(unittest.TestCase):
    """A local link says what it points to beside its ↗ checked without opening it."""

    def test_an_image_shows_its_size_a_report_its_title_and_a_gone_file_says_so(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi.ui import render
        with tempfile.TemporaryDirectory() as folder:
            picture = Path(folder) / "shot.png"
            picture.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1100, 680)
                                + b"\x08\x02\x00\x00\x00")
            report = Path(folder) / "report.md"
            report.write_text("Notes before it\n# ArtLab Rust window fixes: evidence\n\nmore\n")
            self.assertEqual(render.link_hint(str(picture)), "▣ 1100×680")
            self.assertEqual(render.link_hint(str(report)), "ArtLab Rust window fixes: evidence")
            self.assertEqual(render.link_hint(str(Path(folder) / "gone.md")), "missing")
            report.write_text("# Renamed\n")                  # a changed file is read again, not served stale
            os.utime(report, ns=(1, 1))
            self.assertEqual(render.link_hint(str(report)), "Renamed")
            self.assertEqual(render.link_hint(folder), "")        # a folder: no hint, no error


class MentionAndActivityTests(unittest.TestCase):
    """Rules the screen shares with the store, and a status row that must draw whatever the claims hold."""

    def test_the_users_highlight_follows_the_stores_mention_rule(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        mentions = lambda text: ui.Screen.mentions_me({"text": text})
        self.assertTrue(mentions("@user please look"))
        self.assertFalse(mentions("and @someone too"))
        for quiet in ("use `@user` to mention", "```\nnotify @all\n```", "> you wrote @user", "\\@user",
                      "bob@user.com", "@user-agent header"):
            self.assertFalse(mentions(quiet), quiet)

    def test_a_file_claim_that_looks_like_a_task_does_not_break_the_status_row(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.presence = {"claude": {"state": "working"}, "codex": {"state": "listening"}}
        screen.claims = {"task:abc": "claude", "task:²": "claude", "task:12": "claude"}
        screen.attr = {"claude": 0, "codex": 0, "dim": 0}
        self.assertIn("· #12", screen.activity(80)[0][0])
        screen.presence["codex"]["note"] = "reviewing\nthe batch"
        self.assertIn("reviewing the batch", screen.activity(80)[1][0])
        screen.presence["claude"] = {"state": "unknown", "reachable": True}
        self.assertEqual(screen.activity(80)[0][0], "○ claude connected · #12")


class WrapAndShiftTests(unittest.TestCase):
    def test_punctuation_stays_with_a_styled_word(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi.ui.text import wrap
        for width in range(12, 30):
            rows = wrap([], "", [("aaaa bbbb ", 0), ("bold", 1), (". Next (", 0), ("@user", 2), (") ok", 0)], width)
            for row in rows[1:]:
                self.assertFalse("".join(text for text, _ in row).lstrip().startswith((".", ")")), (width, rows))

    def test_visible_characters_edit_whole(self):
        # the UI bug hunt (2026-11-18): the cursor stepped inside 👍🏽, é (e + accent) and ZWJ families
        import sys
        sys.path.insert(0, str(ROOT))
        from agi.ui.text import clusters
        self.assertEqual(clusters("e\u0301x"), [0, 2, 3])
        self.assertEqual(clusters("👍🏽!"), [0, 2, 3])
        self.assertEqual(clusters("👨\u200d👩\u200d👧x"), [0, 5, 6])
        self.assertEqual(clusters("🇳🇱🇧🇪"), [0, 2, 4])
        self.assertEqual(clusters("❤\ufe0f"), [0, 2])

    def test_find_lands_on_a_phrase_that_wrapping_split(self):
        # Codex (#1447): a phrase across two wrapped rows jumped to the message's start instead
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        block = [([("● claude", 0)], None, False), ([("  line one", 0)], None, False),
                 ([("  the quick brown", 0)], None, False), ([("  fox jumps", 0)], None, False)]
        self.assertEqual(ui.Screen._match_row(block, "brown fox"), 2)
        self.assertEqual(ui.Screen._match_row(block, "FOX"), 3)
        self.assertIsNone(ui.Screen._match_row(block, "zebra"))

    def test_a_dead_x_connection_is_noticed_before_xlib_touches_it(self):
        # the UI bug hunt (2026-11-18): Xlib ends the process when a held connection dies
        import socket
        import sys
        from unittest import mock
        sys.path.insert(0, str(ROOT))
        from agi.ui import shiftkey
        ours, theirs = socket.socketpair()
        library = mock.Mock()
        library.XConnectionNumber.return_value = ours.fileno()
        self.assertTrue(shiftkey._alive(object(), library))
        theirs.close()
        self.assertFalse(shiftkey._alive(object(), library))
        ours.close()


class ChipTests(unittest.TestCase):
    """Paste chips edit as one unit and expand exactly once."""

    def screen(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        screen.draft, screen.cursor, screen.history_pos = "", 0, None
        screen.chips, screen.chip_count, screen.chip_numbers = {}, 0, {}
        screen.history, screen.stash, screen.stash_chips, screen.saved_draft = [], "", {}, ("", {}, [])
        screen.notes, screen.help, screen.ctrl_c_at, screen.running = [], False, 0, True
        screen.menu_index, screen.menu_closed, screen.asked = 0, False, False
        screen.win = type("Window", (), {"getmaxyx": lambda self: (24, 80)})()
        return screen

    def test_a_chip_sends_without_its_edge_newlines_and_whitespace_alone_is_not_sent(self):
        # UI hunt (2026-11-18): the draft's edge newlines were trimmed but a chip's own were sent
        from unittest import mock
        from agi import store
        sent = []
        screen = self.screen()
        screen.drafts, screen.view, screen.scroll, screen.unseen = None, None, 0, {}
        with mock.patch.object(store, "append_message", lambda sender, text: sent.append(text)), \
                mock.patch.object(screen, "refresh_data", lambda *a, **k: None, create=True):
            screen.paste("\n\nline 1\n" + "x\n" * 12 + "\n\n")
            screen.submit(screen.draft)
            screen.draft, screen.cursor, screen.chips = "", 0, {}
            screen.paste(" \n" * 12)
            screen.submit(screen.draft)
        self.assertEqual(sent, ["line 1\n" + "x\n" * 11 + "x"])

    def test_a_slow_image_helper_says_it_took_too_long_not_that_the_picture_is_bad(self):
        # T4218, #5258: the helper loads GTK cold each time; under load it ran past 4 s and a good picture read as
        # "couldn't be read as an image". The limit is wider now, and a timeout says what happened
        import subprocess
        from unittest import mock
        from agi.ui import clipboard, composer, pixbuf
        self.assertGreaterEqual(pixbuf.TIMEOUT_S, 15)
        slow = mock.Mock(side_effect=subprocess.TimeoutExpired("python3", pixbuf.TIMEOUT_S))
        with mock.patch.object(pixbuf.subprocess, "run", slow):
            with self.assertRaisesRegex(ValueError, "took too long"):
                composer._attach_file("/tmp/drop.png")
            with self.assertRaisesRegex(ValueError, "took too long"):
                composer._read_clipboard()
            with self.assertRaisesRegex(ValueError, "took too long"):
                clipboard.copy("words")

    def test_an_image_pasted_or_dropped_joins_the_draft_and_sends_with_it(self):
        import tempfile
        from unittest import mock
        from agi import store
        from agi.ui import clipboard, pixbuf
        ref = {"path": "attachments/" + "a" * 64 + ".png", "width": 1200, "height": 800}
        sent = []
        screen = self.screen()
        screen.drafts, screen.view, screen.scroll, screen.unseen = None, None, 0, {}
        screen.images, screen.attaching, screen.interacted = [], 0, False
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(clipboard, "read", lambda: ("image", b"png")), \
                mock.patch.object(pixbuf, "file_png", lambda path: b"png"), \
                mock.patch.object(store, "attach", lambda data: dict(ref)), \
                mock.patch.object(store, "append_message",
                                  lambda sender, text, attachments=None: sent.append((text, attachments))), \
                mock.patch.object(screen, "refresh_data", lambda *a, **k: None, create=True):
            dropped = os.path.join(folder, "drop.png")
            open(dropped, "wb").close()
            screen.key("\x16")                     # Ctrl+V
            screen.paste("")                        # an empty paste: the clipboard holds an image, not text
            screen.paste(f"file://{dropped}\n")     # a file dropped on the terminal pastes its address
            self.assertEqual((len(screen.images), screen.draft), (3, ""))
            self.assertIn("image 3 · 1200×800", screen.image_row(120))
            screen.key("\x7f")                     # Backspace at the start: the last one goes
            self.assertEqual(len(screen.images), 2)
            screen.paste(f"{dropped} is mine")      # text that only mentions a file stays text
            self.assertEqual(screen.draft, f"{dropped} is mine")
            screen.draft, screen.cursor = "", 0
            screen.submit("")                       # images alone are a message
        self.assertEqual(sent, [("", [ref, ref])])
        self.assertEqual(screen.images, [])

    def test_several_files_dropped_at_once_on_one_line_are_all_attached(self):
        # xfce4-terminal drops several files as one line of quoted paths.
        # only one per line was read, so they pasted as text
        from agi.ui.composer import image_files
        with tempfile.TemporaryDirectory() as folder:
            one, two = os.path.join(folder, "a shot.png"), os.path.join(folder, "b.png")
            for path in (one, two):
                open(path, "wb").close()
            self.assertEqual(image_files(f"'{one}' '{two}' "), [one, two])          # as the terminal drops them
            self.assertEqual(image_files(f"{one}\n{two}\n"), [one, two])         # one per line, spaces and all
            self.assertEqual(image_files(f"'{one}'"), [one])
            self.assertEqual(image_files(f"'{one}' is the one"), [])              # text that names a file
            self.assertEqual(image_files(f"'{one}"), [one])                       # a stray quote round one path
            self.assertEqual(image_files(f"'{one}' '{two}"), [])                  # unmatched: not a file list

    def test_pasted_text_that_looks_like_a_label_is_not_expanded_again(self):
        screen = self.screen()
        first = "a" * 1001 + "[Pasted text #2 · 1001 chars]"
        screen.paste(first)
        screen.paste("b" * 1001)
        self.assertEqual(screen.expand(screen.draft), first + "b" * 1001)

    def test_deleting_keeps_line_breaks_and_separate_letters_whole(self):
        # Codex (#1469): Backspace after a leading accent took the line break; a joiner glued "a" to "b";
        # a letter typed before a lone accent left the cursor inside the é it made
        import curses
        screen = self.screen()
        for draft, left in (("hi\n\u0301", "hi\n"), ("a\u200db", "a\u200d"), ("👨\u200d👩!", "👨\u200d👩")):
            screen.draft, screen.cursor = draft, len(draft)
            screen.key(curses.KEY_BACKSPACE)
            self.assertEqual(screen.draft, left, repr(draft))
        screen.draft, screen.cursor = "", 0
        screen.paste("👍\u200d")                                   # Codex (#1472): a chip after an emoji and a
        screen.paste("x" * 1001)                                  # trailing joiner goes alone; so does a CJK
        screen.key(curses.KEY_BACKSPACE)                          # extension letter
        self.assertEqual(screen.draft, "👍\u200d")
        from agi.ui.text import clusters
        self.assertEqual(clusters("👍\u200d\U00020000"), [0, 2, 3])
        screen.draft, screen.cursor, screen.chips = "", 0, {}
        screen.paste("y" * 1001)                                  # Codex (#1474): an accent pasted after a chip
        screen.paste("\u0301")                                    # doesn't join it, so Backspace keeps the paste
        screen.key(curses.KEY_BACKSPACE)
        self.assertEqual(screen.expand(screen.draft), "y" * 1001)
        screen.draft, screen.cursor = "\u0301", 0
        screen.key("e")
        self.assertEqual(screen.cursor, 2)
        screen.key(curses.KEY_BACKSPACE)
        self.assertEqual(screen.draft, "")

    def test_deleting_a_chip_forgets_its_text(self):
        screen = self.screen()
        screen.paste("x" * 5000)
        screen.key("\x7f")
        self.assertEqual((screen.draft, screen.chips), ("", {}))

    def test_a_chip_edits_as_one_unit(self):
        screen = self.screen()
        screen.insert("hi ")
        screen.paste("y" * 2000)
        screen.insert("Z")
        self.assertEqual(len(screen.draft), len("hi Z") + 1)  # the chip is one character in the draft
        screen.cursor = 3
        screen.key(curses_key("KEY_DC"))                      # Delete at the chip removes all of it
        self.assertEqual((screen.draft, screen.chips), ("hi Z", {}))

    def test_literal_unicode_survives_every_input_path(self):
        # Codex's review of the first chip fix: private-use glyphs were dropped, and recall/stash split text from chips
        glyph = "\ue0b0"                                    # a Powerline separator
        screen = self.screen()
        screen.paste(f"prompt {glyph} here")
        self.assertEqual(screen.draft, f"prompt {glyph} here")
        screen = self.screen()
        screen.paste("p" * 1001)                            # a chip...
        chip = next(iter(screen.chips))
        screen.insert(f" and {chip} typed")                 # ...and its own character as literal text
        self.assertEqual(screen.expand(screen.draft), "p" * 1001 + f" and {chip} typed")

    def test_recall_and_stash_move_text_and_chips_together(self):
        screen = self.screen()
        screen.paste("q" * 1001)
        chip = next(iter(screen.chips))
        screen.history = [f"sent earlier with {chip} in it"]   # literal text that equals the parked chip's key
        screen.recall(-1)
        self.assertEqual(screen.expand(screen.draft), f"sent earlier with {chip} in it")
        screen.recall(1)                                    # back to the parked draft, its paste intact
        self.assertEqual(screen.expand(screen.draft), "q" * 1001)
        screen.stash, screen.stash_chips = screen.draft, dict(screen.chips)   # Esc Esc
        screen.draft, screen.cursor, screen.chips = "", 0, {}
        screen.insert("other draft")
        screen.cut(0, 5)                                    # edits elsewhere used to drop the stash's chip
        screen.history = []
        screen.recall(-1)
        self.assertEqual(screen.expand(screen.draft), "q" * 1001)

    def test_typed_text_that_looks_like_a_label_stays_text(self):
        # Codex's audit (#1179): typing a label's text, then pasting, sent the payload twice and lost the text
        screen = self.screen()
        typed = "[Pasted text #1 · 1001 chars] "
        screen.insert(typed)
        screen.paste("p" * 1001)
        self.assertEqual(screen.expand(screen.draft), typed + "p" * 1001)
        self.assertEqual(screen.chip_spans(), [(len(typed), len(typed) + 1)])
        self.assertIn("[Pasted text #1 · 1001 chars]", screen.composer_lines(80)[0][0])   # drawn as its label


class SelectionTests(unittest.TestCase):
    """Drag over the transcript and Ctrl+C copies the words as written."""

    def screen(self, *records, width=40):
        import collections
        from datetime import datetime, timedelta
        screen = ChipTests.screen(self)
        screen.win = type("Window", (), {"getmaxyx": lambda self: (24, width)})()
        screen.attr = collections.defaultdict(int)
        screen.messages, screen.by_id, screen.tasks, screen.refused, screen.marks = collections.deque(), {}, {}, False, {}
        screen.view = screen.new_after = screen.cache_width = None
        screen.scroll, screen.unseen, screen.left, screen.rail_shown = 0, {}, 0, False
        screen.closing = screen.force_quit = False
        start = datetime.now().astimezone() - timedelta(hours=1)
        for number, (sender, text) in enumerate(records, 1):
            screen._add({"id": number, "ts": (start + timedelta(minutes=10 * number)).isoformat(), "sender": sender,
                         "kind": "msg", "text": text})
        return screen

    @staticmethod
    def rows(screen, width=40):
        """Each message's rows as blocks_backward yields them: {id: [(offset, drawn text, Source or not)]}."""
        from agi.ui import render
        return {message["id"]: [(n, "".join(t for t, _ in segments), isinstance(segments, render.Source))
                                for n, (segments, _, _) in enumerate(block)]
                for message, block in screen.blocks_backward(width)}

    def test_whole_lines_copy_as_written_across_wrapping_and_messages(self):
        screen = self.screen(("user", "my first line is long enough to wrap twice at forty columns\nshort one"),
                             ("claude", "- a bullet that wraps past the edge of the screen\nrun `git status` then "
                                        "**push** [the docs](https://example.org/a)"),
                             ("codex", "↪ #2 the reply's own words"))
        rows = self.rows(screen)
        drawn = [text for _, text, _ in rows[1]]
        self.assertTrue(any(text.startswith("  ") and "wrap" in text for text in drawn[1:]), drawn)   # it did wrap
        first = next(n for n, _, source in rows[1] if source)
        last = rows[3][-1][0]
        screen.selection = ((1, first, 0), (3, last, 39))       # from the first word to the end of the last row
        self.assertEqual(screen.selected_text(),
                         "my first line is long enough to wrap twice at forty columns\nshort one\n\n"
                         "- a bullet that wraps past the edge of the screen\n"
                         "run `git status` then **push** [the docs](https://example.org/a)\n\n"
                         "the reply's own words")       # no ❯, names, times, ▌, quote row or ↗ hint
        screen.selection = ((3, last, 39), (1, first, 0))       # dragged upwards: the same text
        self.assertIn("short one", screen.selected_text())

    def test_a_selection_is_one_colour_even_over_coloured_words(self):
        import curses
        screen = self.screen(("claude", "run `git status` **now**"))
        screen.attr["code"] = 6 << 8                     # a colour pair, as curses gives it
        rows = self.rows(screen)
        offset = next(n for n, _, source in rows[1] if source)
        block = next(block for message, block in screen.blocks_backward(40) if message["id"] == 1)
        drawn = []
        screen._put = lambda y, x, text, attr=0: drawn.append((text, attr))
        screen._put_selected(0, block[offset], (0, 40))
        self.assertIn("git status", "".join(text for text, _ in drawn))
        self.assertEqual({attr & curses.A_COLOR for _, attr in drawn}, {0}, drawn)    # no span keeps its colour
        self.assertTrue(all(attr & curses.A_REVERSE for _, attr in drawn))
        self.assertTrue(any(attr & curses.A_BOLD for _, attr in drawn))   # **now** keeps its look

    def test_part_of_a_line_is_the_words_shown_joined_where_they_wrapped(self):
        screen = self.screen(("user", "alpha beta gamma delta epsilon"), width=24)   # 14 cells beside "#1 09:10"
        rows = self.rows(screen, 24)
        drawn = [(n, text) for n, text, source in rows[1] if source]
        self.assertEqual([text.rstrip() for _, text in drawn], ["❯ alpha beta", "  gamma delta", "  epsilon"])
        screen.selection = ((1, drawn[0][0], 8), (1, drawn[1][0], 6))   # from "beta" to the end of "gamma"
        self.assertEqual(screen.selected_text(), "beta gamma")
        screen.selection = ((1, drawn[0][0], 0), (1, drawn[2][0], 5))   # from the ❯ to "epsi"
        self.assertEqual(screen.selected_text(), "alpha beta gamma delta epsi")
        long = "".join(chr(97 + n % 26) for n in range(30))            # a word cut across rows joins with nothing
        screen = self.screen(("user", long), width=24)
        rows = [n for n, _, source in self.rows(screen, 24)[1] if source]
        self.assertEqual(len(rows), 3)                                    # 12 letters a row after the ❯ or indent
        screen.selection = ((1, rows[0], 0), (1, rows[-1], 3))
        self.assertEqual(screen.selected_text(), long[:12 + 12 + 2])

    def test_part_of_a_list_item_leaves_out_its_bullet_and_the_link_hint(self):
        screen = self.screen(("claude", "- see [the docs](https://example.org/a) and more words"), width=60)
        row = next(n for n, _, source in self.rows(screen, 60)[1] if source)
        drawn = dict((n, text) for n, text, _ in self.rows(screen, 60)[1])[row]
        screen.selection = ((1, row, 0), (1, row, drawn.index(" and") + 3))
        self.assertEqual(screen.selected_text(), "see the docs and")

    def test_only_what_the_screen_adds_copies_nothing_and_ctrl_c_still_quits(self):
        from unittest import mock
        from agi.ui import clipboard
        screen = self.screen(("claude", "words"), ("codex", "more words"))
        header = next(n for n, text, _ in self.rows(screen)[2] if text.startswith("● codex"))
        screen.selection = ((2, header, 0), (2, header, 30))      # the name and time row only
        copied = []
        with mock.patch.object(clipboard, "copy", copied.append):
            self.assertTrue(screen.key("\x03"))                   # nothing to copy: the first press of a quit
            self.assertIsNone(screen.selection)
            self.assertFalse(screen.key("\x03"))
        self.assertEqual(copied, [])

    def test_ctrl_c_copies_a_selection_and_then_quits_as_before(self):
        from unittest import mock
        from agi.ui import clipboard
        screen = self.screen(("user", "one\ntwo\nthree"))
        rows = [n for n, _, source in self.rows(screen)[1] if source]
        screen.selection = ((1, rows[0], 0), (1, rows[-1], 39))
        copied = []
        with mock.patch.object(clipboard, "copy", copied.append):
            self.assertTrue(screen.key("\x03"))
            self.assertEqual((copied, screen.notes[0][0], screen.ctrl_c_at), (["one\ntwo\nthree"], "Copied 3 lines", 0))
            self.assertIsNone(screen.selection)                    # copied once: Ctrl+C twice quits again
            self.assertTrue(screen.key("\x03"))
            self.assertFalse(screen.key("\x03"))
        with mock.patch.object(clipboard, "copy", mock.Mock(side_effect=ValueError("no desktop display to copy to"))):
            screen.selection = ((1, rows[0], 0), (1, rows[0], 4))
            screen.key("\x03")
        self.assertEqual(screen.notes[0][:2], ("Not copied: no desktop display to copy to", "error"))

    def test_a_drag_selects_a_click_still_clicks_and_esc_clears(self):
        import curses
        screen = self.screen(("user", "first message here"), ("claude", "second message here"))
        screen.rows_top, screen.shown_rows = 0, screen.body_rows(40, 20)
        texts = ["".join(t for t, _ in segments) for segments, _, _ in screen.shown_rows]
        y1 = next(n for n, text in enumerate(texts) if "first message" in text)
        y2 = next(n for n, text in enumerate(texts) if "second message" in text)
        clicks = []
        screen.clicked = lambda y, x: clicks.append((y, x))
        events = lambda *items: screen.mouse_events.extend((0, x, y, 0, button) for y, x, button in items)
        screen.mouse_events = collections.deque()
        events((y1, 2, curses.BUTTON1_PRESSED), (y1, 2, curses.BUTTON1_RELEASED))
        screen.key(curses.KEY_MOUSE), screen.key(curses.KEY_MOUSE)
        self.assertEqual((clicks, screen.selection), ([(y1, 2)], None))        # a click: '#N', links, replies
        events((y1, 8, curses.BUTTON1_PRESSED), (y2, 4, curses.REPORT_MOUSE_POSITION),
               (y2, 7, curses.REPORT_MOUSE_POSITION), (y2, 7, curses.BUTTON1_RELEASED))
        for _ in range(4):
            screen.key(curses.KEY_MOUSE)
        self.assertEqual(clicks, [(y1, 2)])                                    # a drag is no click
        self.assertEqual(screen.selected_text(), "message here\n\nsecond")
        screen.escape()
        self.assertIsNone(screen.selection)
        events((y1, 8, curses.BUTTON1_PRESSED), (99, 0, curses.REPORT_MOUSE_POSITION))   # below the chat: to its end
        screen.key(curses.KEY_MOUSE), screen.key(curses.KEY_MOUSE)
        self.assertEqual(screen.selected_text(), "message here\n\nsecond message here")
        events((99, 0, curses.BUTTON1_RELEASED))
        screen.key(curses.KEY_MOUSE)
        # read in one go, curses drops a press: its release still clicks, and its first motion starts the drag
        events((y2, 4, curses.BUTTON1_RELEASED))
        screen.key(curses.KEY_MOUSE)
        self.assertEqual((clicks[-1], screen.selection), ((y2, 4), None))
        events((y1, 8, curses.REPORT_MOUSE_POSITION), (y1, 12, curses.REPORT_MOUSE_POSITION))
        screen.key(curses.KEY_MOUSE), screen.key(curses.KEY_MOUSE)
        self.assertEqual(screen.selected_text(), "messa")

    def test_ctrl_v_pastes_the_clipboard_s_text_into_the_draft(self):
        from unittest import mock
        from agi.ui import clipboard
        screen = self.screen()
        screen.images, screen.attaching, screen.interacted = [], 0, False
        with mock.patch.object(clipboard, "read", lambda: ("text", "copied\r\nwords")):
            screen.key("\x16")
        self.assertEqual(screen.draft, "copied\nwords")
        with mock.patch.object(clipboard, "read", lambda: None):
            screen.key("\x16")
        self.assertEqual((screen.draft, screen.notes[0][0]), ("copied\nwords", "The clipboard is empty"))


def curses_key(name):
    import curses
    return getattr(curses, name)


class GroupingTests(unittest.TestCase):
    def test_mixed_timestamp_kinds_never_group_or_crash(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache, screen.by_id = None, {}, {}
        screen.writer, screen.sending = None, None
        aware = {"kind": "msg", "sender": "codex", "text": "a", "ts": "2026-11-16T10:00:00+01:00"}
        naive = dict(aware, ts="2026-11-16T10:00:30")
        self.assertFalse(screen._continues(aware, naive))
        self.assertTrue(screen._continues(aware, dict(aware, ts="2026-11-16T10:01:00+01:00")))


class DraftEditingTests(unittest.TestCase):
    """Up/Down by screen row, edits of recalled entries, and pasted control characters."""

    def screen(self, width=20):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        screen = ChipTests.screen(self)
        screen.history, screen.stash, screen.saved_draft = ["old message"], "", ("", {})
        screen.win = type("Window", (), {"getmaxyx": lambda self: (24, width)})()
        return screen

    def test_question_mark_shows_the_keys_and_still_starts_a_message(self):
        # UI hunt (2026-11-18): ? on an empty draft only toggled the keys, so no message could start with ?
        screen = self.screen()
        screen.key("?")
        self.assertEqual((screen.draft, screen.help), ("?", True))
        for ch in "why":
            screen.key(ch)
        self.assertEqual((screen.draft, screen.help), ("?why", False))
        for close in ("esc", "\r"):                    # only looking: closing the keys takes the ? too
            screen.draft, screen.cursor = "", 0
            screen.key("?")
            screen.escape() if close == "esc" else screen.key(close)
            self.assertEqual((screen.draft, screen.help, screen.running), ("", False, True), close)

    def test_the_cursor_after_a_full_row_starts_the_next_one(self):
        # UI hunt (2026-11-18): it sat on the row's last character, as if that were being replaced
        screen = self.screen()                       # 18 cells a row
        screen.draft, screen.cursor = "a" * 18, 18
        rows, cursor = screen.composer_lines(20)
        self.assertEqual((rows, cursor), (["a" * 18, ""], (1, 0)))
        screen.cursor = 17
        self.assertEqual(screen.composer_lines(20), (["a" * 18], (0, 17)))   # elsewhere: no extra row

    def test_up_and_down_move_through_a_wrapped_line_then_to_its_ends(self):
        screen = self.screen()                       # 18 cells a row: 40 characters wrap onto three rows
        screen.draft, screen.cursor = "a" * 40, 40
        screen.vertical(-1)
        self.assertEqual((screen.draft, screen.cursor), ("a" * 40, 22))
        screen.vertical(-1)
        self.assertEqual(screen.cursor, 4)
        screen.vertical(1)
        self.assertEqual(screen.cursor, 22)
        screen.vertical(-1)
        screen.vertical(-1)                           # past the top row: the start of the draft, not the history
        self.assertEqual((screen.draft, screen.cursor), ("a" * 40, 0))
        screen.vertical(-1)
        self.assertEqual((screen.draft, screen.cursor), ("a" * 40, 0))
        screen.cursor = 30
        screen.vertical(1)                            # past the bottom row: the very end
        self.assertEqual((screen.draft, screen.cursor), ("a" * 40, 40))
        screen = self.screen(width=1)                 # a terminal shrunk to one column: Up crashed the screen
        for draft in ("", "abc", "日本"):              # (playtest, #1941)
            for step in (-1, 1, -1):
                screen.draft, screen.cursor, screen.history_pos = draft, len(draft), None
                screen.vertical(step)
                self.assertTrue(0 <= screen.cursor <= len(screen.draft), (draft, step, screen.cursor))

    def test_up_on_an_empty_draft_recalls_and_down_walks_back(self):
        screen = self.screen()
        screen.vertical(-1)
        self.assertEqual(screen.draft, "old message")
        screen.vertical(-1)                           # recalled from an empty box: Up keeps walking back
        self.assertEqual(screen.draft, "old message")
        screen.vertical(1)
        self.assertEqual(screen.draft, "")

    def test_shift_up_and_down_walk_the_history_with_text_in_the_box(self):
        import curses
        screen = self.screen(width=80)
        screen.draft, screen.cursor = "half typed", 4
        screen.key(curses.KEY_SR)
        self.assertEqual((screen.draft, screen.cursor), ("old message", 11))
        screen.key(curses.KEY_SF)
        self.assertEqual((screen.draft, screen.cursor), ("half typed", 10))   # the draft comes back untouched

    def test_an_edited_recalled_entry_is_not_discarded_by_up(self):
        screen = self.screen(width=80)
        screen.history.append("newer")
        screen.vertical(-1)
        self.assertEqual(screen.draft, "newer")
        screen.cut(len(screen.draft) - 1, len(screen.draft))   # Backspace
        screen.cursor = len(screen.draft)
        screen.recall(-1)                             # browsing again starts afresh from this edit
        screen.recall(1)
        self.assertEqual(screen.draft, "newe")

    def typed(self, draft, cursor=None):
        screen = self.screen(width=80)
        screen.draft, screen.cursor = draft, len(draft) if cursor is None else cursor
        return screen

    def test_a_new_line_continues_a_numbered_list(self):
        for text, expected in (("4. four", "4. four\n5. "), ("9) nine", "9) nine\n10) "), ("  2. x", "  2. x\n  3. "),
                               ("hi\n7. x", "hi\n7. x\n8. ")):
            for press in (lambda s: s.key("\n"), lambda s: s.alt_keys()["\r"]()):    # Ctrl+J, Alt+Enter
                screen = self.typed(text)
                press(screen)
                self.assertEqual((screen.draft, screen.cursor), (expected, len(expected)), text)

    def test_a_new_line_continues_a_bullet_list_and_keeps_the_bullet(self):
        for bullet in ("-", "*", "\u2022"):
            screen = self.typed(f"  {bullet} item")
            screen.newline()
            self.assertEqual(screen.draft, f"  {bullet} item\n  {bullet} ")

    def test_a_lone_first_number_starts_a_list(self):
        for text, expected in (("1.", "1.\n2. "), ("1. ", "1. \n2. "), ("notes\n1)", "notes\n1)\n2) ")):   # #1113
            screen = self.typed(text)
            screen.newline()
            self.assertEqual((screen.draft, screen.cursor), (expected, len(expected)), text)

    def test_a_new_line_on_an_empty_item_ends_the_list(self):
        for text, expected in (("4. a\n5. ", "4. a\n"), ("- a\n  - ", "- a\n")):
            screen = self.typed(text)
            screen.newline()
            self.assertEqual((screen.draft, screen.cursor), (expected, len(expected)))

    def test_a_new_line_after_an_answer_starts_the_next_number(self):
        # Shift+Enter prepares the number for the next choice.
        for text, expected in (("1a", "1a\n2"), ("1b but bluer", "1b but bluer\n2"), ("1a\n2c", "1a\n2c\n3"),
                               ("notes\n1A.", "notes\n1A.\n2"), ("  1a", "  1a\n  2")):
            for press in (lambda s: s.key("\n"), lambda s: s.alt_keys()["\r"](), lambda s: s.newline()):
                screen = self.typed(text)
                press(screen)
                self.assertEqual((screen.draft, screen.cursor), (expected, len(expected)), text)
        screen = self.typed("1a")
        for keys in ("\n", "b", "\n", "c", "\n"):
            screen.newline() if keys == "\n" else screen.insert(keys)
        self.assertEqual(screen.draft, "1a\n2b\n3c\n4")
        for text, expected in (("1a\n2b", "1a\n2b\n3"), ("2b\n1a", "2b\n1a\n2"), ("notes\n1a\n2b\n3c", "notes\n1a\n2b\n3c\n4"),
                               ("1a\n2b\n1c\n2a", "1a\n2b\n1c\n2a\n3")):          # a second run
            screen = self.typed(text)
            screen.newline()
            self.assertEqual(screen.draft, expected, text)

    def test_a_new_line_on_a_lone_next_number_ends_the_answers(self):
        screen = self.typed("1a\n2b\n3")
        screen.newline()
        self.assertEqual((screen.draft, screen.cursor), ("1a\n2b\n", 6))

    def test_words_that_start_with_a_number_and_a_letter_are_not_answers(self):
        for text in ("3d printing", "2b", "1am", "1st", "12ab", "see 1a", "1a\n3b", "2b\n3d printing",
                     "1a\n3b\n4c", "1a\nnotes\n2b"):
            screen = self.typed(text)
            screen.newline()
            self.assertEqual(screen.draft, text + "\n", text)
        screen = self.typed("1a", cursor=1)                          # not at the end of the line
        screen.newline()
        self.assertEqual(screen.draft, "1\na")

    def test_a_new_line_elsewhere_is_plain(self):
        for text, cursor in (("4.5 apples", None), ("-5 degrees", None), ("*bold*", None), ("4. four", 1)):
            screen = self.typed(text, cursor)
            at = screen.cursor
            screen.newline()
            self.assertEqual(screen.draft, text[:at] + "\n" + text[at:], text)

    def test_pasted_lists_are_not_renumbered_and_enter_is_unchanged(self):
        screen = self.typed("4. four")
        screen.paste("\n5. five\n")
        self.assertEqual(screen.draft, "4. four\n5. five\n")
        screen = self.typed("4. four")
        screen.insert("\n")
        self.assertEqual(screen.draft, "4. four\n")

    def test_the_shift_enter_report_continues_the_list(self):
        screen = self.typed("4. four")
        screen.pasting = None
        screen.pending = ["\x1b", "[", "1", "3", ";", "2", "u"]
        screen.interacted = False
        screen.handle_pending()
        self.assertEqual(screen.draft, "4. four\n5. ")

    def test_find_asks_the_store_for_older_messages_only(self):
        from unittest import mock
        from agi import store
        screen = self.screen(width=80)
        screen.messages, screen.view, screen.found = [{"id": 900, "kind": "msg", "text": "recent"}], None, None
        asked = []
        with mock.patch.object(store, "search_messages", lambda *a, **k: asked.append((a, k)) or []), \
                mock.patch.object(screen, "set_highlight", create=True):
            screen.find("needle")
        self.assertEqual(asked, [(("needle",), {"limit": 1, "before_id": 900, "kind": "msg"})])
        self.assertIn("No older message", screen.notes[-1][0])

    def test_pasted_tabs_become_spaces_and_control_codes_go(self):
        screen = self.screen(width=80)
        screen.paste("a\tb\x07c\x1bd\ne")
        self.assertEqual(screen.draft, "a    bcd\ne")


class WhoTests(unittest.TestCase):
    """/who: a row per agent with how fast it was woken for and handled messages."""

    def rows(self, timings, mode=None, state="idle"):
        from unittest import mock
        from agi import store
        screen = ChipTests.screen(self)
        screen.presence, screen.messages = {"claude": {"state": state}}, []
        screen.guard = {"mode": mode, "blocked": mode in ("paused", "wait")}
        with mock.patch.object(store, "get_ack", lambda agent: 0), \
                mock.patch.object(store, "wake_timing", lambda agent: timings.get(agent, {}), create=True):
            return dict(zip(store.AGENTS, screen.who()))

    def test_each_agent_gets_a_row_with_its_last_and_slowest_times(self):
        rows = self.rows({"claude": {"last": {"id": 1604, "wake_s": 2, "ack_s": 40},
                                     "recent": [{"id": 1600, "wake_s": 1, "ack_s": 180}, {"id": 1604, "ack_s": 40}]}})
        self.assertEqual(rows["claude"], ("claude: idle · 0 unread · spoke not yet · #1604 queued 2s, handled 40s"
                                          " · slowest of 2 3m", "dim"))
        self.assertEqual(rows["codex"], ("codex: unknown · 0 unread · spoke not yet", "dim"))   # nothing recorded yet

    def test_a_missed_wake_is_red_only_when_a_wake_was_due(self):
        missed = {"claude": {"oldest_unacked": {"id": 1610, "age": 360, "queued": False},
                             "last": {"id": 1604, "ack_s": 40}}}
        self.assertEqual(self.rows(missed)["claude"],
                         ("claude: idle · 0 unread · spoke not yet · your #1610 unhandled 6m, not queued", "error"))
        self.assertEqual(self.rows(missed, mode="paused")["claude"][1], "dim")   # no wakes while paused
        self.assertEqual(self.rows(missed, state="working")["claude"][1], "dim")  # a turn holds wakes on purpose
        missed["claude"]["oldest_unacked"]["queued"] = True          # queued: it's busy, not lost
        self.assertEqual(self.rows(missed)["claude"][1], "dim")
        self.assertIn("6m, queued", self.rows(missed)["claude"][0])
        for age in (30, None):                                        # too recent, or the clock went backwards
            missed["claude"]["oldest_unacked"]["age"] = age
            self.assertIn("#1604 handled 40s", self.rows(missed)["claude"][0])


class JoinLeaveTests(unittest.TestCase):
    """/join, /leave and /unclaim (#5982, #6005, #6007): one command each, a yes/no where it moves or takes something."""

    def screen(self):
        screen = ChipTests.screen(self)
        screen.confirming = None
        screen.request_status = lambda: None
        screen.drafts, screen.view, screen.scroll, screen.unseen = None, None, 0, {}
        screen.selection = screen.todo_open = screen.todo_focus = screen.rail_focus = None
        return screen

    def run_command(self, screen, text):
        from unittest import mock
        with mock.patch.object(screen, "refresh_data", lambda *a, **k: None, create=True):
            screen.submit(text)

    def test_leave_and_join_call_the_membership_and_say_what_happened(self):
        from unittest import mock
        from agi import membership
        screen = self.screen()
        calls = []
        live = {"session": "s1", "joined": True, "reachable": True, "pending": None, "pending_reachable": False}
        with mock.patch.object(membership, "status", lambda agent: live), \
                mock.patch.object(membership, "leave", lambda agent: calls.append(("leave", agent))), \
                mock.patch.object(membership, "resume", lambda agent, session=None: calls.append(("resume", agent))):
            self.run_command(screen, "/leave claude")
            self.assertIn("Claude left · its claims stay", screen.notes[0][0])
            self.run_command(screen, "/join claude")
            self.assertIn("Claude joined", screen.notes[0][0])
        self.assertEqual(calls, [("leave", "claude"), ("resume", "claude")])

    def test_join_without_a_live_session_says_how_to_start_one(self):
        from unittest import mock
        from agi import membership
        screen = self.screen()
        gone = {"session": "s1", "joined": False, "reachable": False, "pending": None, "pending_reachable": False}
        with mock.patch.object(membership, "status", lambda agent: gone), \
                mock.patch.object(membership, "resume", side_effect=AssertionError("must not resume")):
            self.run_command(screen, "/join claude")
        self.assertIn("No Claude session is connected", screen.notes[0][0])
        self.assertIn("--dangerously-load-development-channels server:agi", screen.notes[0][0])

    def test_a_waiting_session_is_moved_on_enter_and_declined_on_esc(self):
        from unittest import mock
        from agi import membership
        waiting = {"session": "s1", "joined": True, "reachable": True, "pending": "s2", "pending_reachable": True}
        for key, expected in (("\r", ("resume", "s2")), ("esc", ("decline", "s2"))):
            with self.subTest(key):
                screen, calls = self.screen(), []
                with mock.patch.object(membership, "status", lambda agent: waiting), \
                        mock.patch.object(membership, "resume",
                                          lambda agent, session=None: calls.append(("resume", session))), \
                        mock.patch.object(membership, "decline",
                                          lambda agent, session: calls.append(("decline", session))):
                    self.run_command(screen, "/join claude")
                    self.assertIn("move Claude there?", screen.notes[0][0])
                    self.assertIn("Enter yes · Esc no", screen.notes[0][0])
                    self.assertEqual(calls, [])                         # nothing moves before the answer
                    screen.escape() if key == "esc" else screen.key("\r")
                self.assertEqual(calls, [expected])
                self.assertIsNone(screen.confirming)

    def test_unclaim_shows_the_exact_claim_and_releases_only_that_one(self):
        from unittest import mock
        from agi import store
        screen, released = self.screen(), []
        claim = {"claim_id": 812, "owner": "codex", "doing": "running the gate", "since": "now"}
        with mock.patch.object(store, "task_claim", lambda number: claim if number == 800 else None), \
                mock.patch.object(store, "release_task_claim", lambda number, claim_id: released.append((number, claim_id))):
            self.run_command(screen, "/unclaim #800")
            self.assertIn("Take #800 from codex (doing: running the gate)?", screen.notes[0][0])
            screen.key("\r")
            self.assertEqual(released, [(800, 812)])
            self.assertIn("#800 released from codex", screen.notes[0][0])
            self.run_command(screen, "/unclaim #801")
            self.assertIn("Nobody holds #801", screen.notes[0][0])
            self.run_command(screen, "/unclaim #800")
            screen.key("h")                                         # typing instead: the question goes
            screen.draft, screen.cursor = "", 0
            screen.key("\r")
            self.assertEqual(released, [(800, 812)])                # no second release

    def test_the_sidebar_says_left_apart_from_away_and_shows_a_waiting_session(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.claims, screen.attr = {}, {"claude": 0, "codex": 0, "dim": 0}
        member = {"session": "s1", "joined": False, "pending": None, "pending_reachable": False}
        screen.presence = {"claude": {"state": "idle", "reachable": True, "member": member}, "codex": {}}
        self.assertEqual(screen.activity(80)[0][0], "○ claude left · /join claude")
        self.assertEqual(screen.activity(80)[1][0], "○ codex away")
        member.update(joined=True, pending="s2", pending_reachable=True, reachable=True)
        self.assertEqual(screen.activity(80)[0][0], "○ claude idle · another session wants to join: /join claude")
        member.update(reachable=False, pending=None)                    # joined, connection gone (#6130)
        screen.presence["claude"]["reachable"] = False
        self.assertEqual(screen.activity(80)[0][0], "○ claude connection stopped · /who")


class CatchUpAndFindTests(unittest.TestCase):
    """/catchup and /find's list (#6052, #6054): records only, links to open, and nothing marked read."""

    def screen(self):
        screen = JoinLeaveTests.screen(self)
        screen.new_after, screen.marked, screen.found = 100, 100, None
        return screen

    def test_catchup_lists_what_waits_for_you_then_the_rest_and_counts_other_unread(self):
        from unittest import mock
        from agi import history_view
        screen = self.screen()
        view = {"waiting": [{"kind": "decision", "id": 120, "text": "**1a** or 1b?"},
                            {"kind": "todo", "id": 115, "text": "Check the release"}],
                "finished": [{"kind": "task", "id": 110, "text": "Site merged"}],
                "active": [], "other_unread": 41}
        asked = []
        with mock.patch.object(history_view, "catch_up", lambda after: asked.append(after) or view):
            JoinLeaveTests.run_command(self, screen, "/catchup")
        rows = [row[0] for row in screen.notes]
        self.assertEqual(asked, [100])                                  # from your read marker
        self.assertEqual(rows[1], "Waiting for you (2)")
        self.assertIn("  #120 · 1a or 1b?", rows)                       # markup stripped, a link to open
        self.assertIn("  T115 · Check the release", rows)
        self.assertIn("Finished (1)", rows)
        self.assertNotIn("In hand (0)", rows)                           # empty groups aren't shown
        self.assertEqual(rows[-1], "41 other unread messages stay unread below the new line.")
        self.assertEqual(screen.marked, 100)                            # reading it marks nothing read
        self.assertEqual(rows[screen.pick], "  #120 · 1a or 1b?")         # the first link is chosen at once
        opened = []
        screen.open_todo, screen.go_to = (lambda n: opened.append(("todo", n))), (lambda n: opened.append(("msg", n)))
        screen.interacted = False
        screen.key(curses_key("KEY_DOWN"))
        screen.key("\r")
        self.assertEqual(opened, [("todo", 115)])                       # ↓ then Enter opens T115
        self.assertFalse(screen.interacted)                             # the list's keys aren't reading (#6096)

    def test_find_from_a_sender_lists_without_jumping(self):
        from unittest import mock
        from agi import history_view
        screen = self.screen()
        hits = {"messages": [{"id": 90, "sender": "codex", "ts": "2026-11-22T11:25:00+01:00", "text": "the needle"}],
                "more": False}
        with mock.patch.object(history_view, "find", lambda query, before=None, limit=8: hits), \
                mock.patch.object(screen, "find", side_effect=AssertionError("no jump with from:"), create=True):
            JoinLeaveTests.run_command(self, screen, "/find needle from:codex")
        self.assertIn("#90 codex 11:25 · the needle", [row[0] for row in screen.notes])
        # #6103: the from: list saves your place, so Tab after opening a hit comes back here
        self.assertEqual(screen.jumped_from[-1], (None, None))
        screen.go_to = lambda number: screen.jumped_from.append(("hit", number))
        screen.key("\r")
        self.assertEqual(screen.jumped_from[-1], (None, None))

    def test_the_place_is_the_one_before_the_command_menu_took_a_row(self):
        # Codex's split-keystroke run (#6116): top #24 before typing, #25 once the / menu opened, #25 after Tab
        screen = self.screen()
        screen.view, screen.scroll, screen.row_owners, screen.draft = None, 6, [24, 24, 25], ""
        screen.key("/")                                                 # the command starts here
        screen.row_owners = [25, 25, 26]                                # the menu took a row of the chat
        screen.keep_place()
        self.assertEqual(screen.jumped_from[-1], (None, 24))
        self.assertIsNone(screen.command_place)                         # used once

    def test_tab_back_to_the_latest_is_not_reading(self):
        # Codex (#6103): Tab from a catch-up hit back to the live bottom moved the read marker
        screen = self.screen()
        screen.jumped_from, screen.view, screen.interacted = [(None, None)], None, True
        screen.jump_to = lambda *a: False
        screen.go_back()
        self.assertFalse(screen.interacted)


class OwnTodoTests(unittest.TestCase):
    """/todo add (#6054): when it's due is read from the end, shown for a yes/no, never guessed."""

    def test_the_forms_it_reads(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from datetime import datetime, timezone
        from agi.ui import when
        now = datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)          # a Sunday afternoon
        cases = {
            "check the release 30m": ("check the release", datetime(2026, 11, 22, 15, 30, tzinfo=timezone.utc)),
            "call back in 2h": ("call back", datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)),
            "check the release tomorrow 9:00": ("check the release", datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)),
            "review at 9:00": ("review", datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)),   # passed: tomorrow
            "review 17:30": ("review", datetime(2026, 11, 22, 17, 30, tzinfo=timezone.utc)),
            "ship it fri 17:00": ("ship it", datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)),
            "ship it sunday 9": ("ship it", datetime(2026, 11, 29, 9, 0, tzinfo=timezone.utc)),  # today passed
            "taxes 2026-12-01": ("taxes", datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)),
            "read the docs": ("read the docs", None),
            "sunset 9": ("sunset 9", None),                              # not a day: no time, nothing guessed
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(when.parse(text, now, zone=timezone.utc), expected)
        for bad in ("lunch 25:00", "trip 2026-13-40", "tomorrow 9:00", "check 999999999999999d"):
            with self.subTest(bad), self.assertRaises(ValueError):   # refused with guidance, never a crash
                when.parse(bad, now, zone=timezone.utc)

    def test_a_date_across_the_clock_change_keeps_its_own_offset(self):
        # Codex (#6086): typed in November, an April 9:00 must be stored as 9:00 summer time, not 10:00
        import sys
        sys.path.insert(0, str(ROOT))
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from agi.ui import when
        amsterdam = ZoneInfo("Europe/Amsterdam")
        november = datetime(2026, 11, 22, 15, 0, tzinfo=amsterdam)
        _, due = when.parse("Check the report 2027-04-01 9:00", november, zone=amsterdam)
        self.assertEqual(datetime.fromtimestamp(due.timestamp(), amsterdam).strftime("%H:%M %z"), "09:00 +0200")
        with self.assertRaises(ValueError):                             # 2:30 doesn't exist on 28 Mar 2027 there
            when.parse("night job 2027-03-28 2:30", november, zone=amsterdam)

    def test_ctrl_a_in_the_todo_list_starts_an_add_and_letters_still_start_a_message(self):
        # Codex (#6088): a shortcut only in the to-do view; a plain letter there still types (screen audit B2)
        screen = JoinLeaveTests.screen(self)
        screen.todo_focus, screen.todo_open, screen.todo_scroll, screen.todo_said = 1, None, 0, None
        screen.todo_all = [{"id": 1, "result": None}]
        screen.todo_entries = lambda: [(None, None, {"id": 1, "result": None})]
        screen.leave_todo = lambda: setattr(screen, "todo_focus", None)
        self.assertTrue(screen.todo_key("\x01"))
        self.assertEqual((screen.draft, screen.todo_focus), ("/todo add ", None))
        screen.todo_focus, screen.draft = 1, ""
        self.assertFalse(screen.todo_key("a"))                          # not the list's: it goes to the box
        # an empty /todo has no focus, and a message being typed is kept for ↑ (#6096)
        screen.todo_focus, screen.board_kind, screen.cursor = None, "todo", 9
        screen.draft, screen.chips = "half done", {}
        screen.notes = screen.board = [("No to-dos yet.", "dim", None)]
        screen.key("\x01")
        self.assertEqual(screen.draft, "/todo add ")
        self.assertIn("Your message is kept", screen.notes[0][0])

    def test_a_kept_message_comes_back_after_the_todo_is_saved_or_cancelled(self):
        # Codex (#6103): submitting the to-do cleared the stash; now the message returns either way
        from unittest import mock
        from agi import store
        for answer in ("\r", "esc"):
            with self.subTest(answer):
                screen, added = JoinLeaveTests.screen(self), []
                screen.board_kind, screen.todo_focus = "todo", None
                screen.draft, screen.cursor, screen.chips = "half done", 9, {}
                screen.notes = screen.board = [("No to-dos yet.", "dim", None)]
                screen.key("\x01")
                screen.draft = "/todo add water the plants 2h"
                with mock.patch.object(store, "add_personal_todo",
                                       lambda title, due_at=None: added.append(title) or {"id": 9}):
                    JoinLeaveTests.run_command(self, screen, screen.draft)
                    screen.escape() if answer == "esc" else screen.key("\r")
                self.assertEqual(added, ["water the plants"] if answer == "\r" else [])
                self.assertEqual(screen.draft, "half done")             # back in the box

    def test_add_asks_with_the_time_then_saves_on_enter(self):
        from unittest import mock
        from agi import store
        screen, added = JoinLeaveTests.screen(self), []
        with mock.patch.object(store, "add_personal_todo",
                               lambda title, due_at=None: added.append((title, due_at)) or {"id": 7001}):
            JoinLeaveTests.run_command(self, screen, "/todo add water the plants 2h")
            self.assertTrue(screen.notes[0][0].startswith("due "))
            self.assertIn(" · water the plants", screen.notes[0][0])
            self.assertIn("Enter yes · Esc no · Add a to-do for you?", screen.notes[1][0])
            self.assertEqual(added, [])                                  # nothing saved before the yes
            screen.key("\r")
        self.assertEqual(added[0][0], "water the plants")
        self.assertIsNotNone(added[0][1])
        self.assertIn("Added T7001 for you", screen.notes[0][0])


class UpdateAndBackupTests(unittest.TestCase):
    """/update and /backup (#6039, #6041): checked off the keyboard, asked before anything changes, the restart
    only after the screen closes cleanly."""

    def screen(self):
        screen = JoinLeaveTests.screen(self)
        # the thread's work done at once, its result left for the main loop to take, as in the app
        screen.in_background = lambda work, then: setattr(screen, "background", (then, self.attempt(work)))
        return screen

    def settle(self, screen):
        while screen.take_background():
            pass

    @staticmethod
    def attempt(work):
        try:
            return work()
        except Exception as error:
            return error

    def test_an_update_shows_whats_new_asks_installs_then_asks_to_restart(self):
        from types import SimpleNamespace
        from unittest import mock
        from agi import installation
        screen, applied = self.screen(), []
        offer = SimpleNamespace(version="0.1.0-alpha.5")
        with mock.patch.object(installation, "check_update", lambda: offer), \
                mock.patch.object(installation, "update_notes", lambda release: "- Join and leave\n- /catchup"), \
                mock.patch.object(installation, "apply_update",
                                  lambda release: applied.append(release.version) or "/home/x/.local/bin/agi"):
            JoinLeaveTests.run_command(self, screen, "/update")
            self.settle(screen)
            rows = [row[0] for row in screen.notes]
            self.assertEqual(rows[0], "AGI 0.1.0-alpha.5 is out · what's new:")
            self.assertIn("  - Join and leave", rows)
            self.assertIn("Install AGI 0.1.0-alpha.5?", rows[-1])
            self.assertEqual(applied, [])                               # nothing before the yes
            screen.key("\r")
            screen.draft = "Written while installing"
            screen.cursor = len(screen.draft)
            self.settle(screen)
            self.assertEqual(applied, ["0.1.0-alpha.5"])
            self.assertIn("Restart now?", screen.notes[-1][0])
            self.assertTrue(screen.running)
            self.assertFalse(screen.key("\r"))
        self.assertEqual((screen.restart_with, screen.running), ("/home/x/.local/bin/agi", False))
        self.assertEqual(screen.draft, "Written while installing")

    def test_no_update_and_a_failed_check_say_so(self):
        from unittest import mock
        from agi import installation
        screen = self.screen()
        with mock.patch.object(installation, "check_update", lambda: None):
            JoinLeaveTests.run_command(self, screen, "/update")
            self.settle(screen)
        self.assertIn("is the newest release", screen.notes[0][0])
        with mock.patch.object(installation, "check_update", side_effect=ValueError("GitHub unreachable")):
            JoinLeaveTests.run_command(self, screen, "/update")
            self.settle(screen)
        self.assertIn("Couldn't check for an update: GitHub unreachable", screen.notes[0][0])

    def test_backup_says_where_the_zip_went(self):
        from unittest import mock
        from agi import backup
        screen = self.screen()
        with mock.patch.object(backup, "create", lambda: "/home/x/.local/state/agi-backups/agi-20261122.zip"):
            JoinLeaveTests.run_command(self, screen, "/backup")
            self.settle(screen)
        self.assertIn("Backup saved · agi-20261122.zip · in", screen.notes[0][0])


class NotifyAndImageSupportTests(unittest.TestCase):
    """A notification says why it rang (#6057-#6059); a missing GTK for Python is told once, not as "not an image"."""

    def screen(self, *records):
        screen = SelectionTests.screen(self, *records)
        screen.notify_on = True
        screen.notified = []
        screen.desktop = type("Desktop", (), {"notify": lambda me, title, body, message:
                                              screen.notified.append((title, body, message["id"]))})()
        return screen

    def test_a_reply_and_a_mention_have_different_titles(self):
        screen = self.screen(("user", "question"), ("codex", "elsewhere"), ("codex", "answer"), ("codex", "hey @user"))
        screen.messages[2]["reply_to"] = 1
        screen.messages[1]["reply_to"] = 2          # a reply to an agent's message is neither
        screen.desktop_notify([screen.by_id[3]])
        screen.desktop_notify([screen.by_id[4]])
        screen.desktop_notify([screen.by_id[3], screen.by_id[4]])
        self.assertEqual([title for title, _, _ in screen.notified],
                         ["codex replied to you", "codex mentioned you", "codex mentioned you (+1 more)"])
        self.assertEqual(screen.why_addressed(screen.by_id[3]), "answers your message")
        self.assertIsNone(screen.why_addressed(screen.by_id[2]))

    def helper_exit(self, hide):
        import subprocess
        import sys
        code = ("import sys; sys.modules.update(dict.fromkeys(%r)); sys.argv=['x','file','/nonexistent']; "
                "import runpy; runpy.run_module('agi.ui.pixbuf', run_name='__main__')" % hide)
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True).returncode

    def test_the_helper_exits_with_its_own_code_only_when_gi_is_missing(self):
        from agi.ui import pixbuf
        self.assertEqual(self.helper_exit(["gi"]), pixbuf.NO_GTK)
        self.assertEqual(self.helper_exit([]), 0 if self.has_gi() else pixbuf.NO_GTK)   # gi there: no image is not NO_GTK

    @staticmethod
    def has_gi():
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            return True
        except (ImportError, ValueError):
            return False

    def test_load_gtk_and_the_callers_raise_image_support_missing(self):
        import subprocess
        from unittest import mock
        from agi.ui import clipboard, pixbuf
        with mock.patch.dict("sys.modules", {"gi": None}):
            with self.assertRaises(pixbuf.ImageSupportMissing):
                pixbuf.load_gtk(("Gdk", "3.0"))
        done = lambda code: subprocess.CompletedProcess([], code, b"", b"")
        with mock.patch.object(subprocess, "run", lambda *a, **k: done(pixbuf.NO_GTK)):
            with self.assertRaises(pixbuf.ImageSupportMissing):
                pixbuf.file_png("x.png")
            with self.assertRaises(pixbuf.ImageSupportMissing):
                clipboard.read()
        with mock.patch.object(subprocess, "run", lambda *a, **k: done(1)):
            self.assertIsNone(pixbuf.file_png("x.png"))
            self.assertIsNone(clipboard.read())

    def test_the_note_appears_once_and_not_for_other_failures(self):
        import queue
        from agi.ui import pixbuf
        screen = SelectionTests.screen(self)
        screen.writer = type("Writer", (), {"done": queue.Queue()})()
        screen.attaching = 2
        screen.writer.done.put(("attach", None, ValueError("a.png couldn't be read as an image")))
        screen.apply_writes()
        self.assertEqual([note[0] for note in screen.notes], ["Not attached: a.png couldn't be read as an image"])
        screen.writer.done.put(("clipboard", None, None))
        screen.apply_writes()
        self.assertEqual([note[0] for note in screen.notes][-1], "The clipboard is empty")
        # Codex (#6145): Ctrl+V gets the clipboard note (any clipboard, text too), a dropped picture the picture note,
        # each once per run
        shown = []
        for kind in ("clipboard", "attach", "clipboard", "attach"):
            screen.notes.clear()
            screen.writer.done.put((kind, None, pixbuf.ImageSupportMissing()))
            screen.apply_writes()
            shown += [note[0] for note in screen.notes]
        self.assertEqual(shown, [pixbuf.CLIPBOARD_NOTE, pixbuf.MISSING_NOTE])
        self.assertEqual(screen.attaching, 0)


class SettingsTests(unittest.TestCase):
    """/settings (#6043): what's set now, ↑↓ and Enter change it at once, the chat folder shown, the commands kept."""

    def test_enter_changes_the_chosen_setting_and_keeps_the_list_open_on_it(self):
        from unittest import mock
        from agi import ui_state
        from agi.ui import sound
        screen = JoinLeaveTests.screen(self)
        screen.mouse, screen.guard = True, {"mode": "normal"}
        screen.set_mouse = lambda on: setattr(screen, "mouse", on)
        saved = {"notifications": True, "mouse": True}

        def remember(name, enabled):
            saved[name] = enabled
            return dict(saved)
        state = {"sound": "away"}
        with mock.patch.object(ui_state, "user_settings", lambda: dict(saved)), \
                mock.patch.object(ui_state, "set_user_setting", remember), \
                mock.patch.object(sound, "mode", lambda: state["sound"]), \
                mock.patch.object(sound, "set_mode", lambda value: state.update(sound=value)):
            JoinLeaveTests.run_command(self, screen, "/settings")
            rows = [row[0] for row in screen.notes]
            self.assertIn("Sound when you're mentioned: away", rows)
            self.assertIn("Desktop notifications: on", rows)
            self.assertTrue(any(row.startswith("Chat folder: ") for row in rows))
            self.assertEqual(rows[screen.pick], "Sound when you're mentioned: away")
            screen.key("\r")
            self.assertEqual(state["sound"], "always")                   # changed at once
            screen.key(curses_key("KEY_DOWN"))
            screen.key(curses_key("KEY_DOWN"))
            self.assertTrue(screen.notes[screen.pick][0].startswith("Mouse scrolls the chat: on"))
            screen.key("\r")
        self.assertEqual((saved["mouse"], screen.mouse), (False, False))   # saved and applied
        self.assertTrue(screen.notes[screen.pick][0].startswith("Mouse scrolls the chat: off"))  # still on it


class StaleClaimTests(unittest.TestCase):
    """A claim whose owner has been out of touch for over 30 minutes says so on the rail, with the fix (#6043)."""

    def test_the_rail_shows_how_long_and_the_command_and_releases_nothing(self):
        import sys
        import time as clock
        sys.path.insert(0, str(ROOT))
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.attr = {"claude": 1, "codex": 2, "dim": 3, "user": 4, "menu_on": 5}
        screen.rail_focus = screen.rail_open = None
        screen._since = lambda ts: "5m"
        task = {"id": 800, "owner": "codex", "state": "claimed", "text": "Run the gate", "event_ts": "x"}
        screen.presence = {"codex": {"member": {"reachable": False, "last_contact": clock.time() - 41 * 60}}}
        rows = [row[0] for row in screen.rail_task(task, "codex", 2)]
        self.assertIn("  away 41m: /unclaim", rows)
        self.assertTrue(all(len(row) <= 25 for row in rows))
        screen.presence["codex"]["member"]["last_contact"] = clock.time() - 5 * 60   # 5 minutes: nothing said
        self.assertFalse(any("away" in row[0] for row in screen.rail_task(task, "codex", 2)))
        screen.presence["codex"]["member"].update(reachable=True, last_contact=clock.time() - 99 * 60)
        self.assertFalse(any("away" in row[0] for row in screen.rail_task(task, "codex", 2)))


class ConnectionRowTests(unittest.TestCase):
    """/who's line under each agent (#6020, #6107, #6117): left, stopped, unreachable, a test's result."""

    def test_codex_states_each_say_the_one_fix(self):
        from unittest import mock
        from agi import bridge
        screen = JoinLeaveTests.screen(self)
        cases = {"left": "/join codex brings it back", "stopped": "/join codex restarts it",
                 "failed": "the wake for #812 failed", "queued": "woken for #812", "ready": None}
        for state, expected in cases.items():
            with self.subTest(state), mock.patch.object(bridge, "delivery_health",
                                                        lambda: {"state": state, "last_contact": None, "through": 812}):
                row = screen.connection_row("codex")
                self.assertIsNone(row) if expected is None else self.assertIn(expected, row[0])

    def test_claude_is_unverified_until_a_test_comes_back(self):
        import time as clock
        from unittest import mock
        from agi import delivery_probe
        screen = JoinLeaveTests.screen(self)
        screen.presence = {"claude": {"member": {"session": "s1", "joined": True, "reachable": True}}}
        with mock.patch.object(delivery_probe, "status", lambda: {"state": "idle"}):
            self.assertIn("delivery unverified · /who test claude", screen.connection_row("claude")[0])
        with mock.patch.object(delivery_probe, "status",
                               lambda: {"state": "confirmed", "session": "s1", "confirmed_at": clock.time() - 120}):
            self.assertIn("delivery confirmed 2m ago", screen.connection_row("claude")[0])
        screen.presence["claude"]["member"].update(reachable=False, last_contact=clock.time() - 300)
        self.assertIn("connection stopped · last contact 5m ago", screen.connection_row("claude")[0])


class SetupTests(unittest.TestCase):
    """The empty chat (#6027): each client's setup read first, one command to connect, a yes/no before moving a
    registration from another chat."""

    attempt = staticmethod(UpdateAndBackupTests.attempt)

    def screen(self):
        return UpdateAndBackupTests.screen(self)

    def test_the_list_says_what_each_client_needs(self):
        from unittest import mock
        from agi import connect, membership
        screen = self.screen()
        states = {"claude": ("this", None), "codex": ("other", "/elsewhere/chat")}
        with mock.patch.object(connect, "state", lambda client: states[client]), \
                mock.patch.object(membership, "status", lambda agent: {"session": None, "joined": False}):
            screen.show_setup(quiet=True)
            UpdateAndBackupTests.settle(self, screen)
        rows = [row[0] for row in screen.notes]
        self.assertEqual(rows[0], "No agent has joined this chat yet.")
        self.assertIn('Claude Code: set up ✓ · start it with the channel flag', rows[1])
        self.assertEqual(rows[2], "Codex: set up for another chat (/elsewhere/chat) · /connect codex moves it here")

    def test_quiet_at_start_once_an_agent_has_joined(self):
        from unittest import mock
        from agi import connect, membership
        screen = self.screen()
        with mock.patch.object(connect, "state", lambda client: ("this", None)), \
                mock.patch.object(membership, "status",
                                  lambda agent: {"session": "s1" if agent == "claude" else None, "joined": False}):
            screen.show_setup(quiet=True)
            UpdateAndBackupTests.settle(self, screen)
        self.assertEqual(screen.notes, [])

    def test_moving_a_registration_from_another_chat_asks_first(self):
        from unittest import mock
        from agi import connect
        screen, connected = self.screen(), []
        with mock.patch.object(connect, "state", lambda client: ("other", "/elsewhere/chat")), \
                mock.patch.object(connect, "connect",
                                  lambda client, out: connected.append(client) or out("Connected: Codex") or 0):
            JoinLeaveTests.run_command(self, screen, "/connect codex")
            UpdateAndBackupTests.settle(self, screen)
            self.assertIn("set up for another chat (/elsewhere/chat). Point it at this one instead?",
                          screen.notes[-1][0])
            self.assertEqual(connected, [])
            screen.key("\r")
            UpdateAndBackupTests.settle(self, screen)
        self.assertEqual(connected, ["codex"])
        self.assertEqual(screen.notes[0][0], "Connected: Codex")

    def test_a_left_agent_isnt_ticked_and_a_changed_setup_isnt_overwritten(self):
        # Codex (#6123): ✓ only when joined now; the confirmed setup is checked again before writing
        from unittest import mock
        from agi import connect, membership
        screen = self.screen()
        with mock.patch.object(connect, "state", lambda client: ("this", None)), \
                mock.patch.object(membership, "status", lambda agent: {"session": "s1", "joined": False}):
            JoinLeaveTests.run_command(self, screen, "/connect")
            UpdateAndBackupTests.settle(self, screen)
        self.assertIn("Claude Code: set up ✓ · left the chat · /join claude", [row[0] for row in screen.notes])
        seen = iter([("other", "/a"), ("other", "/b")])               # it changes after the confirmation
        connected = []
        with mock.patch.object(connect, "state", lambda client: next(seen)), \
                mock.patch.object(connect, "connect", lambda client, out: connected.append(client) or 0):
            JoinLeaveTests.run_command(self, screen, "/connect codex")
            UpdateAndBackupTests.settle(self, screen)
            screen.key("\r")
            UpdateAndBackupTests.settle(self, screen)
        self.assertEqual(connected, [])
        self.assertIn("setup changed since you looked, so nothing was written", screen.notes[0][0])


class EscHintTests(unittest.TestCase):
    """The bottom rule says what Esc will do when it isn't obvious (#6059), in escape()'s own order."""

    def test_each_state_names_its_esc(self):
        screen = JoinLeaveTests.screen(self)
        screen.selection = screen.todo_open = screen.todo_focus = screen.rail_focus = None
        self.assertIsNone(screen.esc_hint())
        screen.rail_focus = 12
        self.assertEqual(screen.esc_hint(), "esc back to typing")
        screen.todo_open = 4
        self.assertEqual(screen.esc_hint(), "esc back to the list")
        screen.selection = ((0, 0), (0, 3))
        self.assertEqual(screen.esc_hint(), "esc clears the selection")
        screen.confirming = (None, None)
        self.assertIsNone(screen.esc_hint())                            # the question says Esc no itself


class TaskTests(unittest.TestCase):
    """The line under a request and /tasks say only what is true: on it, done, or released."""

    def screen(self, *records):
        from datetime import datetime, timedelta
        screen = ChipTests.screen(self)
        screen.messages, screen.by_id, screen.tasks, screen.refused = [], {}, {}, False
        screen.task_list = screen.todo_list = None
        start = datetime.now().astimezone() - timedelta(minutes=30)
        for number, (sender, kind, text, *done) in enumerate(records, 1):
            record = {"id": number, "ts": (start + timedelta(minutes=number)).isoformat(),
                      "sender": sender, "kind": kind, "text": text}
            if done:
                record["done"] = True
            screen._add(record)
        return screen

    def test_only_a_release_marked_done_reads_as_done(self):
        # Codex (#1606): a release can be a handoff; the line said "done ✓" for every release
        screen = self.screen(("user", "msg", "make the board"), ("claude", "claim", "task:1"),
                             ("claude", "release", "task:1"),
                             ("user", "msg", "fix the water"), ("codex", "claim", "task:4"),
                             ("codex", "release", "task:4", "done"),
                             ("user", "msg", "and the docs"), ("claude", "claim", "task:7"))
        summary = {target: screen._task_summary(events) for target, events in screen.tasks.items()}
        self.assertEqual(summary, {1: "claude took this · released", 4: "codex took this · done ✓",
                                   7: "claude is on it"})

    def test_the_screen_runs_without_admins_entry_point(self):
        # T4669: the public tree leaves out admin's restricted entry point (admin_mentions, bin/admin-mentions); the
        # screen reads only admin_view, so it must import and draw admin's marks (none) without it
        import subprocess
        code = ("import sys; sys.modules['agi.admin_mentions'] = None; sys.path.insert(0, sys.argv[1]); "
                "from agi import admin_view, ui; from agi.ui import app, render; "
                "print(admin_view.addressed_to_admin({'kind': 'msg', 'sender': 'user', 'text': 'hi', 'id': 1}))")
        done = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "False"), done.stderr)

    def test_a_doing_update_never_gets_a_row_even_without_its_request(self):
        # Codex (#5282): once the request has left the history kept, its claim records would show as rows
        from agi.ui.render import Renderer
        update = {"id": 9, "kind": "claim", "sender": "claude", "text": "task:1", "doing_update": True}
        self.assertTrue(Renderer._folded(update, {}))                       # request not in view: still no row
        self.assertFalse(Renderer._folded(dict(update, doing_update=False), {}))   # a real claim: its own row
        self.assertTrue(Renderer._folded(dict(update, doing_update=False), {1: {}}))

    def test_changing_what_an_owner_is_doing_keeps_who_took_it(self):
        # Codex (#5231): a doing edit is a claim record marked doing_update; it changes no ownership
        screen = self.screen(("user", "msg", "rename it"), ("claude", "claim", "task:1"))
        screen._add({"id": 3, "ts": "2026-11-22T06:00:00+01:00", "sender": "claude", "kind": "claim",
                     "text": "task:1", "doing_update": True})
        self.assertEqual(len(screen.tasks[1]), 1)                          # still the one claim
        self.assertEqual(screen._task_summary(screen.tasks[1]), "claude is on it")

    def test_the_board_lists_what_is_in_hand_then_what_ended(self):
        # from store.list_tasks over the whole log (Codex, #1633): newest change first, as the store gives it
        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        ago = lambda minutes: (now - timedelta(minutes=minutes)).isoformat()
        screen = self.screen()
        self.assertEqual(screen.task_rows(), [("Checking tasks…", "dim")])     # before the first status pass
        screen.task_list = []
        self.assertIn("agi take", screen.task_rows()[0][0])
        screen.task_list = [
            {"id": 9, "text": "and the docs", "state": "claimed", "owner": "claude", "result": None, "event_ts": ago(2)},
            {"id": 7, "text": "fix the water", "state": "done", "owner": "codex", "result": "in f4974b8",
             "event_ts": ago(5)},
            {"id": 5, "text": "the board\nplease", "state": "claimed", "owner": "codex", "result": None,
             "event_ts": ago(40)},
            {"id": 1, "text": "make it", "state": "released", "owner": None, "result": None, "event_ts": ago(60)}]
        self.assertEqual([text for text, _ in screen.task_rows()], [
            "#5 codex on it 40m · the board please",                           # longest in hand first
            "#9 claude on it 2m · and the docs",
            "#7 codex done ✓ 5m ago · in f4974b8 · fix the water",
            "#1 released 1h 00m ago · make it"])

    def test_todo_lists_what_waits_on_you_first_with_its_age(self):
        import time
        screen = self.screen()
        screen.attr = {"user": 1, "claude": 2, "codex": 3, "dim": 4}
        self.assertEqual(screen.todo_rows(), [("Checking to-dos…", "dim")])
        screen.todo_list = []
        self.assertIn("agi todo", screen.todo_rows()[0][0])
        from datetime import datetime
        now = time.time()
        iso = lambda ago: datetime.fromtimestamp(now - ago).astimezone().isoformat()   # as the store keeps them
        row = lambda number, title, owner, actor, made, moved, **more: {
            "id": number, "source_id": 4000 + number, "project": "ArtLab", "title": title, "owner": owner,
            "next_action": f"{title}: next", "next_actor": actor, "due_at": None, "reference": None, "result": None,
            "created_at": iso(made), "updated_at": iso(moved), "revision": 1, **more}
        screen.todo_list = [
            row(3, "Properties row test", "claude", "claude", 600, 600),
            row(2, "Second copy of live data", "codex", "user", 3 * 86400, 2 * 86400),
            row(1, "Move the Codex home", "codex", None, 7200, 7200, due_at=None)]
        rows = screen.todo_rows()
        self.assertEqual([style for _, style in rows], ["user", "dim", "claude", "dim", "codex", "dim"])
        self.assertEqual([text.split(" · ")[0] for text, _ in rows],    # number, age first: a narrow window keeps them
                         ["You", "  T2 3d, still 2d", "claude", "  T3 10m", "codex", "  T1 2h"])
        self.assertIn("Second copy of live data · next: Second copy of live data: next", rows[1][0])
        detail = [text for text, _ in screen.todo_detail(screen.todo_list[1], 60)]
        self.assertEqual(detail[0], "Second copy of live data")
        self.assertIn("ArtLab · owner codex · you can act now", detail)
        self.assertIn("made 3d ago, still 2d", detail)
        self.assertIn("from #4002", detail)                          # where it was promised
        self.assertIn("waiting to start", "".join(text for text, _ in screen.todo_detail(screen.todo_list[2], 60)))

    def test_todo_takes_the_keyboard_and_opens_one_in_full(self):
        # Codex (#4125): at 80 columns a line lost its next step, age and source, and a long list its later rows
        import time
        import curses
        from datetime import datetime
        screen = self.screen()
        screen.attr = {"user": 1, "claude": 2, "codex": 3, "dim": 4, "menu_on": 5, "rule": 6}
        screen.notes, screen.board, screen.board_kind = [], None, "tasks"
        jumped = []
        screen.go_to = jumped.append
        made = datetime.fromtimestamp(time.time() - 3600).astimezone().isoformat()
        screen.todo_list = [{"id": n, "source_id": 4000 + n, "project": "AGI", "title": f"item {n} " + "words " * 12,
                             "owner": "claude", "next_action": "a long next step " * 6, "next_actor": None,
                             "due_at": None, "reference": None, "result": None, "created_at": made,
                             "updated_at": made, "revision": 1} for n in range(1, 21)]
        screen.enter_todo()
        self.assertEqual(screen.todo_focus, 1)
        for _ in range(15):
            self.assertTrue(screen.todo_key(curses.KEY_DOWN))
        self.assertEqual(screen.todo_focus, 16)
        rows = screen.todo_view(50, 8)                             # a narrow window, 20 items
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0], ("── to-do · 20 later " + "─" * 28, "rule"))   # a border from the chat (#4195)
        self.assertTrue(rows[-2][0].startswith("›   T16 1h · item 16"), rows)   # the chosen one stays in view
        self.assertEqual(rows[-2][1], "menu_on")
        self.assertIn("Enter opens it", rows[-1][0])
        self.assertTrue(screen.todo_key("\r"))
        whole = " ".join(text for text, _ in screen.todo_view(50, 30))
        self.assertIn("a long next step " * 5, whole.replace("   ", " ") + " ")
        self.assertIn("from #4016", whole)
        screen.escape()                                            # back to the list
        self.assertEqual((screen.todo_open, screen.todo_focus), (None, 16))
        # Codex (#4138): a 482-character next step at 50 columns and 8 rows showed 8 of 18 lines; the rest, the
        # source and the report were out of reach
        long = screen.todo_list[15]
        long["next_action"] = ("Rebuild the release package from the public commit and check it. " * 8)[:482]
        long["reference"] = "/home/example/Documents/Artlab/release.md"
        opened = []
        screen.open_link = opened.append
        self.assertTrue(screen.todo_key("\r"))
        first = screen.todo_view(50, 8)
        self.assertEqual(len(first), 8)
        self.assertTrue(first[-1][0].startswith("↓ ") and "↑↓" in first[-1][0], first[-1])   # narrow: short keys
        for _ in range(30):                                        # down past the end: it stops at the last line
            screen.todo_key(curses.KEY_DOWN)
        last = [text for text, _ in screen.todo_view(50, 8)]
        self.assertIn("from #4016 · see", " ".join(last))
        self.assertFalse(last[-1].startswith("↓"), last)
        self.assertIn("o report", last[-1])
        self.assertTrue(screen.todo_key("o"))
        self.assertEqual(opened, [long["reference"]])
        screen.escape()
        long["reference"] = None
        self.assertTrue(screen.todo_key("\r"))
        self.assertTrue(screen.todo_key("\r"))                    # the second Enter: where it was promised
        self.assertEqual((jumped, screen.todo_focus), ([4016], None))
        # Codex (#4147): T2 open, another agent finishes it, Down, Enter: that crashed on the vanished item
        screen.enter_todo()
        screen.todo_key(curses.KEY_DOWN)
        self.assertTrue(screen.todo_key("\r"))
        self.assertEqual(screen.todo_open, 2)
        screen.todo_list = [todo for todo in screen.todo_list if todo["id"] != 2]   # the next status pass
        self.assertIsNone(screen.todo_view(50, 8) and None)         # drawing it doesn't fail either
        self.assertTrue(screen.todo_key(curses.KEY_DOWN))          # back on the list, choosing
        self.assertIsNone(screen.todo_open)
        self.assertTrue(screen.todo_key("\r"))
        self.assertEqual(screen.todo_open, screen.todo_focus)
        self.assertNotEqual(screen.todo_open, 2)
        screen.leave_todo()
        screen.enter_todo()
        self.assertFalse(screen.todo_key("x"))                     # typing goes back to the box
        self.assertIsNone(screen.todo_focus)

    def test_todo_shows_a_row_the_store_made(self):
        # Codex (#4120): /todo crashed on a real item, whose times are ISO, not the fixture's numbers
        import importlib, os, tempfile, time
        from unittest import mock
        from agi import store
        with tempfile.TemporaryDirectory(prefix="agi-test-", dir=SCRATCH) as folder, \
                mock.patch.dict(os.environ, AGI_DATA=folder):
            importlib.reload(store)
            try:
                source = store.append_message(store.USER, "record the backup decision")
                store.add_todo("codex", source if isinstance(source, int) else source["id"], "Storage",
                               "Second copy of live data", "codex", "the user picks a place", next_actor=store.USER,
                               due_at=time.time() + 86400)
                screen = self.screen()
                screen.attr = {"user": 1, "claude": 2, "codex": 3, "dim": 4}
                screen.todo_list = store.list_todos()
                rows = [text for text, _ in screen.todo_rows()]
            finally:
                importlib.reload(store)
        self.assertEqual(rows[0], "You · 1")
        self.assertIn("T", rows[1].split()[0])
        self.assertIn(" · due ", rows[1])
        self.assertIn("Second copy of live data · next: the user picks a place", rows[1])

    def test_todo_records_read_as_one_line_after_a_clock(self):
        import importlib, os, tempfile, time
        from unittest import mock
        from agi import store
        with tempfile.TemporaryDirectory(prefix="agi-test-", dir=SCRATCH) as folder, \
                mock.patch.dict(os.environ, AGI_DATA=folder):
            importlib.reload(store)
            try:
                source = store.append_message(store.USER, "check the backup next week")
                source = source if isinstance(source, int) else source["id"]
                added = store.add_todo("claude", source, "AGI", "Check the backup", "claude", "look at the logs",
                                       due_at=time.mktime((2026, 11, 28, 9, 0, 0, 0, 0, -1)))
                store.set_next_todo("claude", added["id"])
                store.complete_todo("claude", added["id"], store.list_todos()[0]["revision"], "logs fine, 7 of 7 days")
                records = [m for m in store.read_messages(0, limit=50) if m.get("todo_event") or m.get("todo_next")]
            finally:
                importlib.reload(store)
        screen = self.screen()
        lines = [screen._proto_text(record)[0] for record in records]
        number = added["id"]
        self.assertEqual(lines, [f"T{number} claude · Check the backup · due 28 Nov 09:00",
                                 f"T{number} is claude's next",
                                 f"T{number} done · logs fine, 7 of 7 days"])
        plain = {"id": 1, "sender": "system", "kind": "system", "text": "Rate limit: 5 agent messages per minute"}
        self.assertIsNone(screen._todo_text(plain, plain["text"]))

    def test_each_owner_s_next_leads_their_group_and_n_picks_it(self):
        import time
        import curses
        from datetime import datetime
        from unittest import mock
        from agi import store
        screen = self.screen()
        screen.attr = {"user": 1, "claude": 2, "codex": 3, "dim": 4, "menu_on": 5, "rule": 6, "error": 7}
        made = lambda ago: datetime.fromtimestamp(time.time() - ago).astimezone().isoformat()
        row = lambda n, ago, is_next=False: {"id": n, "source_id": 4000 + n, "project": "AGI", "title": f"item {n}",
                                             "owner": "claude", "next_action": "go", "next_actor": None, "due_at": None,
                                             "reference": None, "result": None, "created_at": made(ago),
                                             "updated_at": made(ago), "revision": 1, "is_next": is_next}
        screen.todo_list = [row(1, 900), row(2, 600, is_next=True), row(3, 300)]
        rows = screen.todo_rows()
        self.assertTrue(rows[1][0].startswith("  T2 10m · up next · item 2"), rows)   # first, though not oldest
        self.assertEqual(rows[1][1], "claude")
        self.assertIn("up next for claude", [text for text, _ in screen.todo_detail(screen.todo_list[1], 60)])
        screen.notes, screen.board, screen.board_kind = [], None, "tasks"
        screen.request_status = lambda: None
        screen.enter_todo()
        screen.todo_key(curses.KEY_DOWN)                           # T1, after the next one
        with mock.patch.object(store, "set_next_todo") as typed:
            # screen audit B2: in the list, "n" is the start of a message ("nice, thanks"), not a command
            self.assertFalse(screen.todo_key("n"))
        typed.assert_not_called()
        screen.enter_todo()
        screen.todo_key(curses.KEY_DOWN)
        self.assertTrue(screen.todo_key("\r"))                    # opened: there "n" makes it next
        with mock.patch.object(store, "set_next_todo", return_value={"id": 1, "owner": "codex"}) as picked:
            self.assertTrue(screen.todo_key("n"))                  # no writer thread: written at once
        picked.assert_called_once_with(store.USER, 1)
        self.assertEqual(screen.todo_view(80, 10)[-1], ("T1 is codex's next", "dim"))   # the saved row's owner
        jobs = []                                                  # Codex (#4239): off the keyboard's thread
        screen.writer = mock.Mock(submit=lambda *job: jobs.append(job))
        self.assertTrue(screen.todo_key("n"))
        self.assertEqual(jobs, [("todo-next", store.set_next_todo, store.USER, 1)])
        self.assertEqual(screen.todo_view(80, 10)[-1], ("Marking T1 next…", "dim"))
        screen.next_todo_result(None, ValueError("Only the owner or the user can change the next to-do"))
        self.assertEqual(screen.todo_view(80, 10)[-1][1], "error")
        screen.next_todo_result({"id": 1, "owner": "claude"}, None)
        self.assertEqual(screen.todo_view(80, 10)[-1], ("T1 is claude's next", "dim"))

    def test_a_t_number_in_a_message_opens_that_to_do_even_when_done(self):
        import time
        from datetime import datetime
        screen = self.screen()
        screen.attr = {"user": 1, "claude": 2, "codex": 3, "dim": 4, "menu_on": 5, "rule": 6, "error": 7,
                       "code": 8, "me": 9, "found": 10}
        screen.highlight, screen.notes, screen.board, screen.board_kind = None, [], None, "tasks"
        out = []
        screen._inline("T4157 is next, see #4156 and AT4 or T42x", 0, out)
        targets = [(text, getattr(attr, "target", None)) for text, attr in out]
        self.assertIn(("T4157", ("todo", 4157)), targets)
        self.assertIn(("#4156", ("message", 4156)), targets)
        self.assertEqual("".join(text for text, _ in out), "T4157 is next, see #4156 and AT4 or T42x")
        self.assertFalse(any(text in ("T4", "T42") for text, _ in targets))    # inside words: not links
        made = datetime.fromtimestamp(time.time() - 600).astimezone().isoformat()
        item = lambda n, result: {"id": n, "source_id": 4000 + n, "project": "AGI", "title": f"item {n}",
                                  "owner": "claude", "next_action": "go", "next_actor": None, "due_at": None,
                                  "reference": None, "result": result, "created_at": made, "updated_at": made,
                                  "revision": 1, "is_next": False}
        screen.todo_all = [item(1, None), item(2, "shipped in db4ee48")]
        screen.todo_list = [screen.todo_all[0]]
        jumped = []
        screen.go_to = jumped.append
        screen.open_todo(2)                                        # done: still opens, with its result
        self.assertEqual(screen.todo_open, 2)
        shown = [text for text, _ in screen.todo_view(80, 12)]
        self.assertIn("done · shipped in db4ee48", shown)
        self.assertTrue(screen.todo_key("\r"))                    # Enter: where it was promised
        self.assertEqual(jumped, [4002])
        # Codex (#4280): a long result was cut at 50 columns; it wraps with the other fields
        screen.todo_all[1]["result"] = "shipped in db4ee48 after the Mini gate, reviewed twice, with the store and the screen together"
        screen.open_todo(2)
        result = " ".join(text.strip() for text, _ in screen.todo_view(50, 20))
        self.assertIn("with the store and the screen together", result)
        # and the T number on a ◷ line opens it, as in a message
        segments = screen._refs("T4157 claude · First daily review · see #4156", 4)
        self.assertEqual([(text, getattr(attr, "target", None)) for text, attr in segments],
                         [("T4157", ("todo", 4157)), (" claude · First daily review · see ", None), ("#4156", ("message", 4156))])
        self.assertEqual(screen._refs("Rate limit", 4), [("Rate limit", 4)])
        screen.leave_todo()
        screen.open_todo(99)                                        # not a to-do: said, nothing opened
        self.assertIsNone(screen.todo_open)
        self.assertIn("T99 isn't a to-do here", screen.notes[-1][0])

    def test_an_answer_inside_a_message_names_whose_message_it_answers(self):
        screen = self.screen()
        screen.attr = {"user": 1, "claude": 2, "codex": 3, "dim": 4, "me": 5, "code": 6, "found": 7}
        screen.highlight = None
        screen.by_id = {4294: {"id": 4294, "sender": "user", "kind": "msg", "text": "a question"},
                        4296: {"id": 4296, "sender": "codex", "kind": "msg", "text": "a note"}}
        out = []
        screen._inline("1. done ↪ #4294 Yes, it is. ↪ #4296 Agreed. See #4296 and ↪ #9999", 0, out)
        text = "".join(piece for piece, _ in out)
        self.assertEqual(text, "1. done ↪ #4294 you Yes, it is. ↪ #4296 codex Agreed. See #4296 and ↪ #9999")
        self.assertEqual(next(attr for piece, attr in out if piece == " you"), 5)

    def test_a_send_in_flight_moves_nothing_and_the_jump_hint_is_a_button(self):
        # right and couldn't be clicked
        screen = self.screen()
        screen.notes, screen.sending = [], ("draft", 0)
        self.assertTrue(screen.busy())
        self.assertEqual(screen.notes, [])                         # no row added: the chat stays where it was
        screen.rail_shown, screen.left, screen.rows_top, screen.shown_rows = False, 0, 0, []
        screen.view, screen.scroll, screen.unseen = None, 12, {7: "codex", 8: "codex", 9: "codex"}
        screen.menu_closed, screen.draft = False, "/"               # Codex (#4373): with the / menu open
        screen.hint_spot = (20, 30, 52)                            # " ▼ 3 new · Esc to jump " in the middle
        self.assertFalse(screen.clicked(20, 10))                   # beside it: nothing
        self.assertTrue(screen.clicked(20, 40))
        self.assertEqual((screen.scroll, screen.unseen), (0, {}))   # at the latest in one click, menu or not

    def test_a_released_request_says_what_it_waits_on_and_its_numbers_open(self):
        from datetime import datetime
        screen = self.screen()                                     # sets up the import path first (#4406)
        from agi.ui.text import waiting_for
        now = datetime.now().astimezone().isoformat()
        self.assertEqual(waiting_for({"handoff_to": "codex"}), "→ codex")
        self.assertEqual(waiting_for({"deferred_todos": [4171, 4159]}), "waits T4171 T4159")
        self.assertEqual(waiting_for({}), "no owner")
        screen.task_list = [{"id": 3211, "text": "the docs", "state": "released", "owner": None, "event_ts": now,
                             "note": "Waiting for T4326", "deferred_todos": [4326]},
                            {"id": 3990, "text": "rust folder", "state": "released", "owner": None, "event_ts": now,
                             "handoff_to": "codex"}]
        rows = [text for text, _ in screen.task_rows()]
        self.assertIn("#3211 waits T4326 · the docs", rows)        # said once, not "waits T4326 · Waiting for T4326"
        self.assertIn("#3990 → codex · rust folder", rows)
        screen.task_list[0]["note"] = "Waiting for the combined gate before picking this up"   # a real note stays
        self.assertIn("#3211 waits T4326 · Waiting for the combined gate before picking this up · the docs",
                      [text for text, _ in screen.task_rows()])
        opened, jumped = [], []
        screen.open_todo, screen.go_to = opened.append, jumped.append
        screen.rail_shown, screen.left, screen.rows_top, screen.shown_rows = False, 0, 0, []
        screen.note_spots = [(30, 2, 7, "message", 3211), (30, 14, 19, "todo", 4326)]   # as the notes drew them
        self.assertTrue(screen.clicked(30, 15))
        self.assertTrue(screen.clicked(30, 3))
        self.assertEqual((opened, jumped), ([4326], [3211]))
        # a number cut short at a narrow width isn't a link: "T41…" mustn't open T41 (#4406)
        self.assertEqual(screen.note_links("see T4326 and T41…", 2), [(6, 11, "todo", 4326)])
        self.assertEqual(screen.note_links("#9 waits T41… · the docs", 2), [(2, 4, "message", 9)])   # mid-row too
        self.assertEqual(screen.note_links("#3211 waits T4326", 2), [(2, 7, "message", 3211), (14, 19, "todo", 4326)])

    def test_a_task_names_its_helpers_after_its_driver(self):
        from datetime import datetime
        screen = self.screen()
        now = datetime.now().astimezone().isoformat()
        screen.task_list = [
            {"id": 7, "text": "the to-do", "state": "claimed", "owner": "claude", "helpers": ["claude", "codex"],
             "event_ts": now},
            {"id": 3, "text": "the icon", "state": "done", "owner": "codex", "helpers": ["claude"], "result": "ok",
             "event_ts": now}]
        rows = [text for text, _ in screen.task_rows()]
        self.assertTrue(rows[0].startswith("#7 claude +codex on it"), rows)   # the driver once, then the others
        self.assertTrue(rows[1].startswith("#3 codex done ✓"), rows)          # an ended one: just who closed it

    def test_a_claim_held_over_a_day_takes_the_attention_colour(self):
        # Both the rail and /tasks show stale claims.
        import collections
        from datetime import datetime, timedelta
        screen = self.screen()
        screen.attr = collections.defaultdict(int, {"user": 11, "claude": 22, "codex": 33, "dim": 44})
        fresh = datetime.now().astimezone().isoformat()
        old = (datetime.now().astimezone() - timedelta(hours=26)).isoformat()
        screen.task_list = [{"id": 9, "text": "today's", "state": "claimed", "owner": "codex", "event_ts": fresh},
                            {"id": 4, "text": "forgotten", "state": "claimed", "owner": "claude", "event_ts": old}]
        screen.rail_sorted = (None, [])
        colours = {task["id"]: attr for task, _, attr in screen.rail_tasks()}
        self.assertEqual(colours, {9: 33, 4: 11})
        rows = [text for text, _ in screen.task_rows()]
        self.assertIn("· over a day", next(row for row in rows if row.startswith("#4 ")))
        self.assertNotIn("over a day", next(row for row in rows if row.startswith("#9 ")))

    def test_a_claim_shows_what_its_owner_is_doing_now(self):
        # outside change twice (Codex's store gives `doing` on an active claim, #5226)
        from datetime import datetime
        screen = self.screen()
        now = datetime.now().astimezone().isoformat()
        doing = "renaming the GitHub repositories"
        screen.task_list = [
            {"id": 5202, "text": "rename it", "state": "claimed", "owner": "claude", "doing": doing, "event_ts": now},
            {"id": 7, "text": "the docs", "state": "claimed", "owner": "codex", "event_ts": now},
            {"id": 3, "text": "the icon", "state": "done", "owner": "codex", "doing": "stale", "event_ts": now}]
        rows = [text for text, _ in screen.task_rows()]
        self.assertIn(f"#5202 claude on it", rows[-2])
        self.assertIn(f"· ▸ {doing} · rename it", rows[-2])
        self.assertNotIn("▸", rows[-1] + rows[-3])          # no doing: nothing extra; an ended claim: never shown
        screen.attr = collections.defaultdict(int)
        screen.rail_open = screen.rail_focus = None
        header, line, words = screen.rail_task(screen.task_list[0], "claude", 0)
        self.assertEqual((line[0], words[0]), (f"  ▸ {doing}", "  rename it"))
        self.assertEqual(len(screen.rail_task(screen.task_list[1], "codex", 0)), 2)   # header and words only
        screen.rail_open = 5202                                                         # opened: all of it, wrapped
        opened = [text for text, _, _ in screen.rail_task(screen.task_list[0], "claude", 0)]
        self.assertTrue(opened[1].startswith("  ▸ renaming"), opened)
        self.assertIn("repositories", " ".join(opened))

    def test_handoff_keeps_its_next_step_above_recent_completions(self):
        from datetime import datetime
        screen = self.screen()
        now = datetime.now().astimezone().isoformat()
        screen.task_list = [
            {"id": number, "text": "Finished", "owner": "codex", "state": "done", "event_ts": now}
            for number in range(9, 5, -1)
        ] + [{"id": 1, "text": "Check the release", "owner": None, "state": "released", "event_ts": now,
              "note": "Run Win11\n and check the hash"}]
        rows = [text for text, _ in screen.task_rows()]
        self.assertEqual(rows[0], "#1 handoff · Run Win11 and check the hash · Check the release")
        self.assertEqual(len(rows), 4)


class ReplyCommandTests(unittest.TestCase):
    def test_only_the_exact_reply_command_is_rewritten(self):
        import sys
        from unittest import mock
        sys.path.insert(0, str(ROOT))
        from agi import store, ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        screen.draft, screen.cursor, screen.history, screen.notes = "", 0, [], []
        screen.history_pos, screen.stash, screen.scroll, screen.unseen = None, "", 0, {}
        screen.chips, screen.chip_count, screen.drafts, screen.view = {}, 0, None, None
        screen.running, screen.claims, screen.presence, screen.found = True, {}, {}, None
        sent = []
        with mock.patch.object(store, "append_message", lambda *a: sent.append(a[1])), \
                mock.patch.object(screen, "refresh_data", create=True):
            screen.submit("/reply 7 sure")
            screen.submit("  /reply 8   with spaces")
            screen.submit("/reply 9\nfrom a new line")
            screen.submit("/REPLY\t#10 thanks")
            screen.submit("/reply")                 # bare, and number-only: usage, not a crash
            screen.submit("/reply 42")
            screen.submit("/reply7 nope")
            screen.submit("/replying")
        self.assertEqual(sent, ["↪ #7 sure", "↪ #8 with spaces", "↪ #9 from a new line", "↪ #10 thanks"])
        self.assertIn("Unknown command", screen.notes[0][0])


class ModeTests(unittest.TestCase):
    """Mode commands and the footer, against a stand-in for Codex's set_policy API."""

    def screen(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        screen.notes, screen.refused = [], False
        screen.attr = {"dim": 1, "error": 2}
        return screen

    def test_mode_commands_call_the_policy(self):
        from unittest import mock
        from agi import store
        screen, calls = self.screen(), []
        policy = lambda mode, **kw: calls.append((mode, kw)) or {"mode": mode}
        with mock.patch.object(store, "set_policy", policy, create=True), \
                mock.patch.object(store, "resume_policy", lambda: calls.append(("resume", {})) or {}, create=True):
            for command, argument in [("/custom", "10 1 60"), ("/custom", "4 1"), ("/custom", "off"), ("/mode", "wait"),
                                      ("/pause", ""), ("/resume", ""), ("/custom", "2h"), ("/custom", "4"),
                                      ("/custom", "0 1 5")]:
                screen.set_mode(command, argument)
        self.assertEqual(calls, [("custom", {"limit": 10, "window_s": 60, "duration_s": 3600}),
                                 ("custom", {"limit": 4, "window_s": 60, "duration_s": None}),   # no end
                                 ("normal", {}), ("wait", {}), ("paused", {}), ("resume", {})])
        self.assertIn("Usage: /custom 10 1 60", screen.notes[-1][0])     # the last two were rejected

    def test_footer_describes_each_mode(self):
        screen = self.screen()
        screen.set_guard({"mode": "normal", "used": 1, "message_limit": 5, "window_seconds": 60})
        self.assertEqual(screen.footer()[0], "normal · 1/5 per min")
        screen.set_guard({"mode": "auto", "used": 3, "message_limit": 10, "window_seconds": 120,
                          "expires_at": 1794837600, "blocked": True, "retry_after": 30})
        self.assertTrue(screen.footer()[0].startswith("custom · 3/10 per 2 min until "))
        self.assertRegex(screen.footer()[0], r"full, agents wait (29|30)s")   # counting down from 30
        screen.set_guard({"mode": "custom", "used": 0, "message_limit": 4, "window_seconds": 60, "expires_at": None})
        self.assertEqual(screen.footer()[0], "custom · 0/4 per min")          # no end, so no "until"
        screen.set_guard({"mode": "quiet"})
        self.assertEqual(screen.footer()[0], "paused · agents can't post · /resume")


class ModifiedKeyTests(unittest.TestCase):
    """Modified-key reports (kitty CSI u, xterm modifyOtherKeys) decode to the keys the chat already knows."""

    def test_reports_decode_to_familiar_keys(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi.ui.keys import decode
        cases = {
            "[13;2u": ("newline",), "[27;2;13~": ("newline",), "[13;3u": ("newline",), "[13;5u": ("newline",),
            "[13u": ("key", "\r"), "[57414;2u": ("newline",), "[27u": ("escape",), "[99;5u": ("key", "\x03"),
            "[106;5u": ("key", "\n"), "[98;3u": ("alt", "b"), "[127;3u": ("alt", "\x7f"), "[127u": ("key", "\x7f"),
            "[9u": ("key", "\t"), "[97u": ("key", "a"), "[27;5;99~": ("key", "\x03"),
            "[200~": None, "[>1u": None, "[2~": None, "[15~": None, "[57399;5u": None, "[P": None,
        }
        import curses
        cases.update({                  # cursor keys, sent as CSI in kitty's mode and normal cursor mode (#754)
            "[A": ("key", curses.KEY_UP), "[B": ("key", curses.KEY_DOWN), "OC": ("key", curses.KEY_RIGHT),
            "[D": ("key", curses.KEY_LEFT), "[H": ("key", curses.KEY_HOME), "OF": ("key", curses.KEY_END),
            "[1~": ("key", curses.KEY_HOME), "[4~": ("key", curses.KEY_END), "[3~": ("key", curses.KEY_DC),
            "[3;5~": ("key", curses.KEY_DC), "[5~": ("key", curses.KEY_PPAGE), "[6~": ("key", curses.KEY_NPAGE),
            "[1;5D": ("alt", "b"), "[1;3C": ("alt", "f"),
            "[1;2A": ("key", curses.KEY_SR), "[1;2B": ("key", curses.KEY_SF),     # Shift, Alt, Ctrl + Up/Down walk
            "[1;3A": ("key", curses.KEY_SR), "[1;3B": ("key", curses.KEY_SF),     # the history (xterm and kitty)
            "[1;5A": ("key", curses.KEY_SR), "[1;5B": ("key", curses.KEY_SF), "[1;2:1A": ("key", curses.KEY_SR),
        })
        for sequence, expected in cases.items():
            self.assertEqual(decode(sequence), expected, sequence)


class MentionTests(unittest.TestCase):
    def test_only_exact_mentions_of_the_user_count(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agi import ui
        def mine(text):
            return ui.Screen.mentions_me({"text": text})
        self.assertTrue(mine("@user look"))
        self.assertTrue(mine("ping @ALL."))
        self.assertFalse(mine("@user-name is someone else"))
        self.assertFalse(mine("@all-team"))
        self.assertFalse(mine("mail user@all.example"))


class ReadMarkLogicTests(unittest.TestCase):
    """Which marks each message gets, without a terminal."""

    def screen(self, messages, acks=None, wakes=None):
        import sys
        from collections import deque
        sys.path.insert(0, str(ROOT))
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.messages, screen.by_id = deque(messages), {m["id"]: m for m in messages}
        screen.cache, screen.marks, screen.acks, screen.wakes = {}, {}, acks or {}, wakes or {}
        screen.update_marks()
        return screen

    @staticmethod
    def message(number, sender="user", text="hi", kind="msg"):
        return {"id": number, "ts": "2026-11-20T09:00:00+01:00", "sender": sender, "kind": kind, "text": text}

    def test_a_user_message_gets_a_dot_per_agent_once_delivered_then_their_check_once_acked(self):
        log = [self.message(1)]
        self.assertEqual(self.screen(log).marks, {})                                 # not delivered: nothing
        screen = self.screen(log, wakes={"claude": ((1, 1),)})
        self.assertEqual(screen.marks, {1: (("claude", False),)})
        self.assertEqual(screen._meta(log[0]), "○ #1 09:00")
        screen = self.screen(log, {"claude": 1}, {"claude": ((1, 1),), "codex": ((1, 1),)})
        self.assertEqual(screen.marks, {1: (("claude", True), ("codex", False))})
        self.assertEqual(screen._meta(log[0]), "✓ ○ #1 09:00")
        screen = self.screen(log, {"claude": 1, "codex": 1})                          # an ack alone shows too
        self.assertEqual(screen._meta(log[0]), "✓ ✓ #1 09:00")

    def test_marks_stay_on_older_messages_and_a_batch_receipt_covers_its_whole_range(self):
        log = [self.message(1), self.message(2, "claude", "chatter"), self.message(3)]
        screen = self.screen(log, {"codex": 1}, {"codex": ((1, 3),)})
        self.assertEqual(screen.marks, {1: (("codex", True),), 3: (("codex", False),)})
        screen = self.screen(log, {"codex": 3})
        self.assertEqual(screen.marks, {1: (("codex", True),), 3: (("codex", True),)})

    def test_an_agent_message_marks_only_the_agents_it_mentions_and_chatter_none(self):
        log = [self.message(1, "codex", "@claude can you check"), self.message(2, "claude", "done, thanks"),
               self.message(3, "claude", "@all heads up"), self.message(4, "codex", "see `@claude` in code")]
        screen = self.screen(log, {"claude": 4, "codex": 4})
        self.assertEqual(screen.marks, {1: (("claude", True),), 3: (("codex", True),)})   # not the sender, not chatter

    def test_the_admin_is_marked_only_for_at_admin_and_only_once_acked(self):
        log = [self.message(1, text="hello"), self.message(2, text="@admin please"), self.message(3, "claude", "@admin you too")]
        screen = self.screen(log, {"admin": 2}, {"claude": ((1, 3),), "codex": ((1, 3),)})
        self.assertEqual(screen.marks[2], (("claude", False), ("codex", False), ("admin", True)))
        self.assertEqual(screen.marks[1], (("claude", False), ("codex", False)))
        self.assertEqual(screen.marks.get(3), None)                                    # admin not yet acked, no dot stage
        self.assertEqual(self.screen(log, {"admin": 3}).marks[3], (("admin", True),))

    def test_system_lines_and_task_events_get_nothing(self):
        log = [self.message(1, "system", "@claude @codex rate limit"), self.message(2, "user", "ask"),
               self.message(3, "claude", "task:2", "claim"), self.message(4, "claude", "task:2", "release")]
        screen = self.screen(log, {"claude": 4, "codex": 4, "admin": 4})
        self.assertEqual(set(screen.marks), {2})

    def test_marks_invalidate_only_the_rows_that_change(self):
        log = [self.message(1), self.message(2, "claude", "@codex")]
        screen = self.screen(log, {"codex": 1})
        screen.cache[1] = screen.cache[2] = "rows"
        screen.update_marks()
        self.assertEqual(screen.cache, {1: "rows", 2: "rows"})
        screen.wakes = {"codex": ((2, 2),)}
        screen.update_marks()
        self.assertEqual(screen.cache, {1: "rows"})
        self.assertEqual(screen._meta(log[1]), "○ #2 09:00")
        screen.cache[2] = "rows"
        screen.acks = {"codex": 2}
        screen.update_marks()
        self.assertEqual(screen.marks, {1: (("codex", True),), 2: (("codex", True),)})
        self.assertEqual(screen.cache, {1: "rows"})
        self.assertEqual(screen._meta(log[1]), "✓ #2 09:00")

        screen = self.screen(log, {"codex": 1}, {"codex": ((2, 2),)})
        screen.cache.update({1: "rows", 2: "rows"})
        screen.wakes = {}
        screen.update_marks()
        self.assertEqual(screen.cache, {1: "rows"})
        self.assertEqual(screen._meta(log[1]), "#2 09:00")


class ScreenCase(unittest.TestCase):
    """Starts bin/agi in a pty with its own throwaway data. Holds no tests itself."""
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="agi-ui-", dir=SCRATCH)
        self.data = os.path.join(self.folder.name, "data")
        self.screen = None
        self.others = []                    # extra screens a test opens; stopped before the folder goes

    def tearDown(self):
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None)
        failed = result and any(test is self for test, _ in result.failures + result.errors)
        if failed and self.screen:          # leave evidence: what the screen showed when the test gave up
            import sys
            print(f"\n--- screen at failure of {self.id()} ---\n{self.screen.term.text()}\n--- end ---",
                  file=sys.stderr)
        for screen in [self.screen] + self.others:
            if not screen:
                continue
            try:
                os.kill(screen.pid, signal.SIGKILL)
                os.waitpid(screen.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
            try:
                os.close(screen.fd)             # the pty master; reopened screens (crash tests) close theirs too
            except OSError:
                pass
        self.folder.cleanup()               # only now: a live screen could still be writing into it

    def open(self, count=0, **options):
        if count:
            seed(self.data, count)
        self.screen = ChatScreen(self.data, **options)
        return self.screen


class ChatScreenTests(ScreenCase):
    """Functional screen tests; safe to run alongside other suites."""
    def test_short_task_board_keeps_the_handoff_and_composer_visible(self):
        screen = self.open()
        request = screen.chat("say", "--from", "user", "Verify the new build")["id"]
        screen.chat("take", "--as", "codex", str(request))
        screen.chat("release-task", "--as", "codex", str(request), "--note", "Review café hash 検証 " * 6,
                    "--result", "half done", "--check", "reviewed by hand", "--skills", "none, because a fixture")
        for number in range(3):
            task = screen.chat("say", "--from", "user", f"Later request {number}")["id"]
            screen.chat("take", "--as", "claude", str(task))
            screen.chat("done", "--as", "claude", str(task), "--result", "done", "--check", "a fixture",
                        "--skills", "none, because a fixture")
        screen.resize(8, 30)
        screen.type("/tasks\r")
        self.assertTrue(screen.until(lambda: f"#{request} handoff · Review café" in screen.term.text()))
        self.assertIn("❯", screen.term.text())

    def test_gates_is_read_only_and_dismisses_on_the_next_key(self):
        screen = self.open(rows=8, cols=30, AGI_GATES="1")          # the one test that reads this machine's gates
        screen.type("/gates\r")
        # the board's answer, whatever this machine's gates are: a project's row from a git checkout, or
        # "Integration status unavailable" (cut short at 30 columns) from a copy without .git; T5066: the sweep's
        # git-archive copy waited 15 s for "AGI ·". gates.snapshot's rows themselves are tested in test_integrate
        answered = lambda text: "Checking" not in text and ("AGI ·" in text or "Integration status" in text)
        self.assertTrue(screen.until(lambda: answered(screen.term.text())))
        self.assertIn("❯", screen.term.text())
        self.assertEqual(screen.messages(), [])
        screen.type("x")
        self.assertTrue(screen.until(lambda: "❯ x" in screen.term.text()))
        self.assertNotIn("AGI ·", screen.term.text())
        self.assertNotIn("Integration status", screen.term.text())

    def test_a_dropped_image_is_attached_sent_and_shown_as_a_link(self):
        import struct
        import zlib

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        picture = Path(self.data).parent / "drop.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 2, 8, 2, 0, 0, 0))
                            + chunk(b"IDAT", zlib.compress(b"\0" + b"\xff\x80\x00" * 3 + b"\0" + b"\x00\x80\xff" * 3))
                            + chunk(b"IEND", b""))
        screen = self.open()
        screen.type(f"\x1b[200~{picture}\x1b[201~")
        self.assertTrue(screen.until(lambda: "image 1 · 3×2" in screen.term.text()))
        screen.type("look\r")
        self.assertTrue(screen.until(lambda: screen.messages()))
        message = screen.messages()[0]
        self.assertEqual((message["text"], message["attachments"][0]["width"]), ("look", 3))
        self.assertTrue(screen.until(lambda: "▣ image · 3×2" in screen.term.text()))

    def test_paste_stays_in_the_draft_and_sends_once(self):
        screen = self.open()
        screen.type("\x1b[20", 0.25)        # a start marker split across reads
        screen.type("0~first line\rsecond line\x1b[201~")
        self.assertTrue(screen.until(lambda: "second line" in screen.term.text()))
        self.assertEqual(screen.messages(), [])
        screen.type("\r")
        self.assertTrue(screen.until(lambda: screen.messages()))
        self.assertEqual([m["text"] for m in screen.messages()], ["first line\nsecond line"])

    def test_large_paste_becomes_a_chip_and_sends_in_full(self):
        screen = self.open()
        big = "\n".join(f"line {n} " + "x" * 60 for n in range(200))
        start = time.time()
        screen.type("\x1b[200~" + big + "\x1b[201~")
        self.assertTrue(screen.pump_until("[Pasted text #1 · 200 lines]"))
        screen.type("a")
        self.assertLess(time.time() - start, 3)
        screen.type("\x7f\r")
        self.assertTrue(screen.until(lambda: screen.messages()), "the send never reached the log")
        self.assertEqual(screen.messages()[-1]["text"], big)

    def test_pasted_code_keeps_its_indentation(self):
        screen = self.open()
        screen.type("\x1b[200~    def f():\n        return 1\n\x1b[201~\r")
        screen.until(lambda: screen.messages())
        self.assertEqual(screen.messages()[-1]["text"], "    def f():\n        return 1")

    def test_failed_send_keeps_the_draft(self):
        screen = self.open()
        screen.chat("say", "--from", "user", "first")
        screen.until(lambda: "first" in screen.term.text())
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        log.chmod(0o444)
        screen.type("keep this text")
        screen.type("\r")
        self.assertTrue(screen.until(lambda: "Not sent" in screen.term.text()))
        log.chmod(0o644)
        self.assertIn("keep this text", screen.term.text())
        screen.type("\r")
        screen.until(lambda: screen.messages()[-1]["text"] == "keep this text")
        self.assertEqual(screen.messages()[-1]["text"], "keep this text")

    def test_mentions_of_the_user_are_marked(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "@user please look at this")
        self.assertTrue(screen.until(lambda: "▌ @user please look" in screen.term.text()))
        self.assertNotIn("→ you", screen.term.text())   # the bar marks it; a badge as well was one blob

    def test_resizing_down_and_up_redraws(self):
        screen = self.open(50)
        screen.pump(0.5)
        start = len(screen.raw)
        screen.resize(12, 40)
        self.assertIn("❯", screen.term.text())
        screen.pump(0.5)
        outside = re.sub(rb"\x1b\[\?2026h.*?\x1b\[\?2026l", b"", screen.raw[start:], flags=re.S)
        self.assertNotIn(b"\x1b[2J", outside)       # every repaint is inside a frame: no blank flash between
        self.assertIn(b"\x1b[2J", screen.raw[start:])
        screen.resize(24, 80)
        self.assertIn("❯", screen.term.text())
        self.assertGreater(sum(1 for line in screen.term.text().splitlines() if line.strip()), 12)

    def test_task_claims_fold_under_the_request(self):
        screen = self.open()
        request = screen.chat("say", "--from", "user", "@claude fix the bug")
        screen.chat("take", "--as", "claude", str(request["id"]))
        self.assertTrue(screen.pump_until("claude is on it"))
        self.assertNotIn("claimed task:", screen.term.text())

    def test_exits_when_the_terminal_disappears(self):
        screen = self.open()
        os.close(screen.fd)                 # the terminal window closes without sending SIGHUP
        os.kill(screen.pid, signal.SIGCONT)
        deadline = time.time() + 20         # it ends within a second; the margin is for a busy host
        while time.time() < deadline:
            done, _ = os.waitpid(screen.pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.1)
        self.assertTrue(done, "chat screen kept running after its terminal closed")

    def test_light_markdown_is_rendered(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "Run `agi show 5` **now**\n- first item\n```py\nx = 1\n```")
        self.assertTrue(screen.until(lambda: "• first item" in screen.term.text()))
        text = screen.term.text()
        self.assertIn("Run agi show 5 now", text)
        self.assertIn("│ x = 1", text)
        self.assertNotIn("```", text)

    def test_long_agent_messages_show_in_full(self):
        screen = self.open()
        screen.chat("say", "--from", "claude", "\n".join(f"long message row {n:02d}" for n in range(40)))
        self.assertTrue(screen.until(lambda: "long message row 39" in screen.term.text()))
        screen.type("\x1b[5~" * 6)
        self.assertTrue(screen.until(lambda: "long message row 00" in screen.term.text()))
        screen.type("/find long message row 21\r")
        self.assertTrue(screen.until(lambda: "Found #" in screen.term.text()
                                     and "long message row 21" in screen.term.text()))

    def test_find_lists_older_hits_and_arrows_open_one_then_tab_returns(self):
        # Codex (#6083): the ordinary /find route end to end: the jump, the list of older hits, ↓ and Enter to
        # open one, and Tab back to where the list was opened from
        seed(self.data, 40)
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        lines = log.read_text().splitlines()
        for number, words in ((3, "an old needle three"), (12, "a needle twelve"), (35, "the newest needle")):
            lines[number - 1] = json.dumps(dict(json.loads(lines[number - 1]), text=words))
        log.write_text("\n".join(lines) + "\n")
        screen = self.open()
        self.assertTrue(screen.until(lambda: "#40" in screen.term.text()))
        screen.type("/find needle\r")
        self.assertTrue(screen.until(lambda: "the newest needle" in screen.term.text()
                                     and "#12 " in screen.term.text() and "#3 " in screen.term.text()))
        screen.type("\x1b[B")                                           # ↓: the second older hit, #3
        screen.type("\r")
        self.assertTrue(screen.until(lambda: "an old needle three" in screen.term.text()))
        screen.type("\t")                                               # Tab: back where /find started (#6096),
        self.assertTrue(screen.until(lambda: "#40" in screen.term.text()     # the latest, not the newest hit
                                     and "an old needle three" not in screen.term.text()))

    def test_typing_catchup_at_the_live_bottom_leaves_the_read_marker(self):
        # Codex's keyboard run (#6113): from the live bottom, typing /catchup moved the saved marker #10 → #48
        # while the view said 36 messages stay unread
        seed(self.data, 48)
        ui = Path(self.data) / "ui"
        ui.mkdir(mode=0o700, exist_ok=True)
        (ui / "read.json").write_text(json.dumps({"version": 1, "read_through": 10}))
        screen = self.open()
        self.assertTrue(screen.until(lambda: "#48" in screen.term.text()))
        screen.type("/catchup\r")
        self.assertTrue(screen.until(lambda: "other unread" in screen.term.text()))
        screen.pump(1.5)                                                # the marker is saved on a timer
        self.assertEqual(json.loads((ui / "read.json").read_text())["read_through"], 10)
        screen.type("\x1b")
        screen.pump(1.5)
        self.assertEqual(json.loads((ui / "read.json").read_text())["read_through"], 10)

    def test_find_jumps_to_an_older_message(self):
        seed(self.data, 31)                 # written straight to the log: 31 CLI posts took ~6 s
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        lines = log.read_text().splitlines()
        lines[0] = json.dumps(dict(json.loads(lines[0]), text="the needle is here"))
        log.write_text("\n".join(lines) + "\n")
        screen = self.open()
        self.assertTrue(screen.until(lambda: "#31" in screen.term.text()))
        self.assertNotIn("the needle is here", screen.term.text())
        screen.type("/find NEEDLE\r")
        self.assertTrue(screen.until(lambda: "the needle is here" in screen.term.text()
                                     and "Found #1" in screen.term.text()))

    def test_unsent_draft_survives_a_crash(self):
        screen = self.open()
        screen.type("half-written thought")
        drafts = Path(self.data) / "ui" / "drafts"
        saved = lambda: any("half-written thought" in path.read_text() for path in drafts.glob("*.json"))
        self.assertTrue(screen.until(saved, 5))     # the draft saves about a second after the last edit
        os.kill(screen.pid, signal.SIGKILL)
        os.waitpid(screen.pid, 0)
        self.screen = again = ChatScreen(self.data)
        self.assertTrue(again.until(lambda: "Restored your unsent draft" in again.term.text()
                                    and "half-written thought" in again.term.text()))

    def test_new_rule_marks_where_the_user_stopped_reading(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "already read")
        screen.until(lambda: "already read" in screen.term.text())
        screen.type("x\x7f")               # interacting marks everything so far as read
        screen.pump(0.3)
        self.assertEqual(screen.quit(), 0)
        screen.chat("say", "--from", "claude", "arrived while away")
        self.screen = again = ChatScreen(self.data)
        self.assertTrue(again.until(lambda: "arrived while away" in again.term.text()))
        lines = again.term.text().splitlines()
        rule = next(n for n, line in enumerate(lines) if " new " in line and "─" in line)
        self.assertLess(next(n for n, line in enumerate(lines) if "already read" in line), rule)
        self.assertGreater(next(n for n, line in enumerate(lines) if "arrived while away" in line), rule)

    def test_arrivals_while_away_stay_unread_even_after_a_signal(self):
        screen = self.open(60)              # enough history that PageUp really scrolls
        screen.chat("say", "--from", "codex", "seen this one")
        screen.until(lambda: "seen this one" in screen.term.text())
        screen.type("x\x7f")               # an interaction at the bottom: #1 is seen
        screen.pump(0.3)
        screen.chat("say", "--from", "claude", "came in while away")
        screen.until(lambda: "came in while away" in screen.term.text())
        screen.type("\x1b[5~")             # scrolled up: keys here don't mark live arrivals read
        self.assertTrue(screen.until(lambda: "Esc to jump" in screen.term.text()))
        screen.type("y\x7f")
        screen.pump(2.5)                    # past the 2 s write throttle: nothing may be written now
        os.kill(screen.pid, signal.SIGTERM)
        os.waitpid(screen.pid, 0)
        self.screen = again = ChatScreen(self.data)
        self.assertTrue(again.until(lambda: "came in while away" in again.term.text()))
        lines = again.term.text().splitlines()
        rule = next(n for n, line in enumerate(lines) if " new " in line and "─" in line)
        self.assertLess(next(n for n, line in enumerate(lines) if "seen this one" in line), rule)
        self.assertGreater(next(n for n, line in enumerate(lines) if "came in while away" in line), rule)

    def test_find_opens_history_older_than_the_screen_holds(self):
        seed(self.data, 1200)               # the screen keeps 1,000; #5 is outside it
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        lines = log.read_text().splitlines()
        record = json.loads(lines[4])
        record["text"] = "ancient needle here"
        lines[4] = json.dumps(record)
        log.write_text("\n".join(lines) + "\n")
        screen = self.open()
        screen.type("/find ancient needle\r")
        # wait for the whole frame: the match can be on screen before the hint row of the same draw arrives
        self.assertTrue(screen.until(lambda: "ancient needle here" in screen.term.text()
                                     and "History view" in screen.term.text()))
        screen.type("draft kept separately")
        self.assertTrue(screen.until(lambda: "draft kept separately" in screen.term.text()))
        self.assertEqual(screen.quit(), 0)
        self.others.append(screen)
        screen.chat("say", "--from", "user", "latest after the first close")

        self.screen = again = ChatScreen(self.data, cols=55)
        self.assertTrue(again.until(lambda: all(text in again.term.text() for text in (
            "ancient needle here", "History view", "draft kept separately"))))
        self.assertNotIn("latest after the first close", again.term.text())
        again.type("\x1b")
        self.assertTrue(again.until(lambda: "History view" not in again.term.text()
                                    and "latest after the first close" in again.term.text()))
        self.assertEqual(again.quit(), 0)
        self.others.append(again)
        again.chat("say", "--from", "user", "latest after the second close")

        self.screen = latest = ChatScreen(self.data)
        self.assertTrue(latest.until(lambda: "latest after the second close" in latest.term.text()))
        self.assertNotIn("History view", latest.term.text())

    def test_a_note_goes_on_the_rule_and_moves_nothing_above_it(self):
        # when it cleared, and a click right after landed a row off
        screen = self.open()
        screen.chat("say", "--from", "codex", "steady line")
        self.assertTrue(screen.until(lambda: "steady line" in screen.term.text()))
        before = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(before) if "steady line" in line)
        screen.type("/mouse on\r")               # a one-line note
        self.assertTrue(screen.until(lambda: "Scrolling moves the chat" in screen.term.text()))
        after = screen.term.text().splitlines()
        self.assertIn("steady line", after[row])                                  # nothing moved
        noted = next(line for line in after if "Scrolling moves the chat" in line)
        self.assertIn("─", noted)                                                 # it sits on the rule

    def test_the_message_a_reply_answers_keeps_a_row_of_its_own(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "@user a question worth answering at some length")
        self.assertTrue(screen.until(lambda: "worth answering" in screen.term.text()))
        screen.type("/reply")
        screen.type("\t")                       # Tab picks the newest message that mentions you
        self.assertTrue(screen.until(lambda: "#1 codex:" in screen.term.text()))
        quoted = next(line for line in screen.term.text().splitlines() if "#1 codex:" in line)
        self.assertNotIn("─", quoted)
        screen.type("yes, ")                    # screen audit B4: it stays while you write the reply
        self.assertTrue(screen.until(lambda: "yes," in screen.term.text()))
        screen.pump(0.3)
        self.assertIn("#1 codex:", screen.term.text())

    def test_whats_new_shows_once_until_read_or_hidden(self):
        # T4270 on Codex's T4177 receipts: after an update, one line on the rule until /whatsnew or Esc; then
        # never again for this version, also after a restart
        from agi import updates
        if updates.current_release() is None:
            self.skipTest("this checkout's version has no dated changelog entry")
        version = updates.current_release()["version"]
        screen = self.open(AGI_RELEASE_NOTICE="1")
        self.assertTrue(screen.until(lambda: f"new in AGI {version}" in screen.term.text()))
        screen.type("/whatsnew\r")
        # read: the notes show and the line is gone, in one frame (checked mid-frame, the gate once saw the notes
        # drawn above the rule before the rule itself was redrawn)
        self.assertTrue(screen.until(lambda: "Esc closes" in screen.term.text()
                                     and f"new in AGI {version}" not in screen.term.text()))
        screen.type("\x1b")
        self.assertTrue(screen.until(lambda: "Esc closes" not in screen.term.text()))
        screen.quit()
        again = self.open(AGI_RELEASE_NOTICE="1")                         # a restart: still read
        again.pump(0.5)
        self.assertNotIn(f"new in AGI {version}", again.term.text())
        again.type("/whatsnew\r")                                       # and it opens again on request
        self.assertTrue(again.until(lambda: f"AGI {version} ·" in again.term.text()))

    def test_esc_hides_the_new_version_line_for_good(self):
        from agi import updates
        if updates.current_release() is None:
            self.skipTest("this checkout's version has no dated changelog entry")
        version = updates.current_release()["version"]
        screen = self.open(AGI_RELEASE_NOTICE="1")
        self.assertTrue(screen.until(lambda: f"new in AGI {version}" in screen.term.text()))
        screen.type("\x1b")
        self.assertTrue(screen.until(lambda: f"new in AGI {version}" not in screen.term.text()))
        screen.quit()
        again = self.open(AGI_RELEASE_NOTICE="1")
        again.pump(0.5)
        self.assertNotIn(f"new in AGI {version}", again.term.text())

    def test_decisions_lists_the_picks_and_where_they_were_given(self):
        # Marked answers keep the original reply numbers and any qualification.
        screen = self.open()
        screen.chat("decision", "ask", "--as", "claude", "--project", "AGI", "--choice", "1a", "--choice", "1b",
                    "--choice", "2a", "--choice", "2b", "Two questions: 1a or 1b, 2a or 2b")
        screen.chat("say", "--from", "user", "↪ #1 1. b but only on Linux\n2. a")
        self.assertTrue(screen.until(lambda: "only on Linux" in screen.term.text()))
        screen.type("/decisions\r")
        self.assertTrue(screen.until(lambda: "#1 AGI · 1b✎ 2a · in #2" in screen.term.text()))
        self.assertIn("Two questions", screen.term.text())
        screen.chat("decision", "ask", "--as", "codex", "--project", "Artlab", "--choice", "1a", "--choice", "1b",
                    "Another one")
        screen.chat("say", "--from", "user", "↪ #3 maybe a or b")
        screen.type("\x1b")
        screen.type("/decisions\r")
        self.assertTrue(screen.until(lambda: "#3 Artlab · no answer yet · needs a look: #4" in screen.term.text()))

    def test_a_check_on_a_request_follows_its_holder_and_can_be_cancelled(self):
        # T5655 (#5649 6a, Codex #5771): /remind's cases from /todo on the screen, as to-dos that follow a request.
        # The gate form needs a commit the store can find and that isn't on main yet, so the check stays pending: a
        # throwaway repository with one commit on main and one beside it (Codex, #5802), never the real checkout
        repo = Path(self.folder.name) / "gate-repo"
        git = lambda *args: subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                                           check=True).stdout.strip()
        repo.mkdir()
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "base")
        git("checkout", "-q", "-b", "candidate")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "next")
        sha = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        screen = self.open(AGI_GATE_REPO=str(repo))
        screen.chat("say", "--from", "user", "please look at the build")                 # 1
        screen.chat("take", "--as", "claude", "1")
        screen.type("/todo #1 30m look again\r")
        self.assertTrue(screen.until(lambda: "checks #1 at" in screen.term.text()))
        listed = json.loads(screen.chat("todo", "list").stdout)
        check = next(item for item in listed if item["source_id"] == 1)
        self.assertEqual((check["owner"], check["title"]), ("claude", "look again"))
        screen.type("/todo #1 45m\r")                       # a new time reschedules it, no second check (#5775)
        self.assertTrue(screen.until(lambda: f"T{check['id']} now checks #1 at" in screen.term.text()))
        self.assertEqual(sum(item["source_id"] == 1 for item in json.loads(screen.chat("todo", "list").stdout)), 1)
        screen.type(f"/todo #1 gate agi {sha}\r")             # from a time to a commit: the date goes
        self.assertTrue(screen.until(lambda: f"T{check['id']} now checks #1 when {sha[:7]}" in screen.term.text()))
        moved = next(item for item in json.loads(screen.chat("todo", "list").stdout) if item["id"] == check["id"])
        self.assertEqual((moved["project"], moved["due_at"]), ("AGI", None))
        screen.type(f"/todo #1 gate agi {sha}\r")             # the same again: nothing to write (#5785)
        self.assertTrue(screen.until(lambda: f"T{check['id']} already checks #1" in screen.term.text()))
        screen.chat("todo", "add", "--as", "claude", "--owner", "claude", "--source", "1", "--project", "AGI",
                    "--title", "Another check", "--next-action", "x", "--next-actor", "claude", "--follow-request")
        screen.type("/todo #1 off\r")                       # two now: it doesn't guess which (#5781)
        self.assertTrue(screen.until(lambda: "several checks" in screen.term.text()))
        self.assertEqual(sum(item["source_id"] == 1 for item in json.loads(screen.chat("todo", "list").stdout)), 2)
        other = next(item for item in json.loads(screen.chat("todo", "list").stdout) if item["id"] != check["id"])
        screen.chat("todo", "done", "--as", "claude", "--revision", str(other["revision"]), "--result", "x",
                    str(other["id"]))
        screen.type("/todo #1 off\r")
        self.assertTrue(screen.until(lambda: f"Cancelled T{check['id']}" in screen.term.text()))
        screen.type("/todo #7 30m\r")                                                   # nobody holds it
        self.assertTrue(screen.until(lambda: "Nobody holds #7" in screen.term.text()))

    def test_mentions_notify_the_desktop_only_when_unfocused(self):
        record = os.path.join(self.folder.name, "notified.txt")
        fake = os.path.join(self.folder.name, "fake-notify")
        Path(fake).write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" >> {record}\necho >> {record}\n")
        os.chmod(fake, 0o755)
        screen = self.open(AGI_NOTIFY_CMD=fake)
        screen.type("\x1b[O")              # the terminal window loses focus
        screen.chat("say", "--from", "codex", "@user are you there?")
        self.assertTrue(screen.until(lambda: os.path.exists(record) and "are you there" in Path(record).read_text()))
        self.assertIn("codex mentioned you", Path(record).read_text())
        screen.type("\x1b[I")              # focus comes back: no more notifications
        screen.chat("say", "--from", "claude", "@user second ping")
        self.assertTrue(screen.until(lambda: "second ping" in screen.term.text()))
        screen.pump(0.3)
        self.assertNotIn("second ping", Path(record).read_text())
        self.assertNotIn("[O", screen.term.text())   # focus reports never leak into the draft

    def test_the_mention_sound_plays_while_away_always_or_never_as_sound_says(self):
        # the popup, so only while away (#5435), and an option for while AGI is open (#5440); remembered
        # its own sound file: a runner without the desktop's sounds stays silent, which is right, but tests nothing
        tone = os.path.join(self.folder.name, "tone.oga")
        Path(tone).write_bytes(b"")
        played = os.path.join(self.folder.name, "played.txt")
        player = os.path.join(self.folder.name, "fake-player")
        Path(player).write_text(f"#!/bin/sh\necho \"$1\" >> {played}\n")
        os.chmod(player, 0o755)
        quiet = os.path.join(self.folder.name, "fake-notify")
        Path(quiet).write_text("#!/bin/sh\n")
        os.chmod(quiet, 0o755)
        count = lambda: len(Path(played).read_text().split()) if os.path.exists(played) else 0
        screen = self.open(AGI_NOTIFY_CMD=quiet, AGI_SOUND_CMD=player, AGI_SOUND_FILE=tone)
        screen.type("\x1b[I")              # looking at the chat: by default a mention makes no sound
        screen.chat("say", "--from", "codex", "@user first")
        self.assertTrue(screen.until(lambda: "first" in screen.term.text()))
        screen.pump(0.3)
        self.assertEqual(count(), 0)
        screen.type("\x1b[O")              # away: one sound, the desktop's own
        screen.chat("say", "--from", "codex", "@user second")
        self.assertTrue(screen.until(lambda: count() == 1))
        self.assertEqual(Path(played).read_text().split(), [tone])
        screen.type("\x1b[I")
        screen.type("/sound always\r")     # also while AGI is in front
        self.assertTrue(screen.until(lambda: "Sound on for every mention" in screen.term.text()))
        screen.chat("say", "--from", "codex", "@user third")
        self.assertTrue(screen.until(lambda: count() == 2))
        # The sound log in the chat's data folder gives a reason for each entry.
        logged = (Path(self.data) / "ui" / "sounds.log").read_text()
        self.assertRegex(logged, r"sound and popup #2 from codex \(msg\): for you while away")
        self.assertRegex(logged, r"sound \(always\) #3 from codex \(msg\): mentions you")
        self.assertRegex(logged, r"terminal bell #3 from codex \(msg\): mentions you")
        screen.type("/sound off\r")
        self.assertTrue(screen.until(lambda: "Sound off" in screen.term.text()))
        screen.quit()
        again = self.open(AGI_NOTIFY_CMD=quiet, AGI_SOUND_CMD=player, AGI_SOUND_FILE=tone)   # remembered across a restart
        again.type("\x1b[O")
        again.chat("say", "--from", "claude", "@user fourth")
        self.assertTrue(again.until(lambda: "fourth" in again.term.text()))
        again.pump(0.3)
        self.assertEqual(count(), 2)

    def test_modified_key_reports_are_requested_decoded_and_restored(self):
        screen = self.open()
        self.assertTrue(screen.until(lambda: b"\x1b[>1u\x1b[>4;1m" in screen.raw))   # asked for at start
        screen.type("/")
        self.assertTrue(screen.until(lambda: "/reply" in screen.term.text()))
        screen.type("\x1b[27u")            # kitty's Esc closes the menu, as Esc does
        self.assertTrue(screen.until(lambda: "/reply" not in screen.term.text()))
        screen.type("\x7f")                # the "/" that opened it
        screen.type("one")
        screen.type("\x1b[27;2;13~")       # xterm's Shift+Enter
        screen.type("two")
        screen.type("\x1b[106;5u")         # kitty's Ctrl+J
        screen.type("three\x1b[13;3u")     # kitty's Alt+Enter
        self.assertTrue(screen.until(lambda: "three" in screen.term.text()))
        screen.type("\x1b[13u")            # kitty's plain Enter sends
        self.assertTrue(screen.until(lambda: screen.messages()))
        self.assertEqual([m["text"] for m in screen.messages()], ["one\ntwo\nthree"])
        screen.type("\x1b[99;5u")          # kitty's Ctrl+C, twice: quits
        screen.type("\x1b[99;5u")
        self.assertTrue(screen.until(lambda: b"\x1b[<u\x1b[>4m" in screen.raw, 10))     # and restored on the way out

    def test_arrow_keys_sent_as_csi_move_through_the_text(self):
        # Asked for kitty's mode, terminals send arrows as CSI A rather than the SS3 form curses decodes (#754)
        screen = self.open()
        self.assertTrue(screen.until(lambda: b"\x1b[>1u" in screen.raw))
        screen.type("one two")
        screen.type("\x1b[1;5D")          # Ctrl+Left: back a word
        screen.type("big ")
        screen.type("\x1b[F\nthree")      # End, a new line
        screen.type("\x1b[A\x1b[H")       # Up, Home
        screen.type("> ")
        screen.type("\x1bOB\x1b[4~")      # Down (SS3), End (numbered)
        screen.type("!\x1b[D\x1b[D\x1b[3~")   # two back, Delete
        self.assertTrue(screen.until(lambda: "three!" not in screen.term.text() and "thre!" in screen.term.text()))
        screen.type("\r")
        self.assertTrue(screen.until(lambda: screen.messages()))
        self.assertEqual([m["text"] for m in screen.messages()], ["> one big two\nthre!"])

    def test_tab_after_reply_fills_in_messages_to_the_user_newest_first(self):
        screen = self.open()
        mine = screen.chat("say", "--from", "user", "a question")["id"]
        mention = screen.chat("say", "--from", "codex", "@user have a look")["id"]
        screen.chat("say", "--from", "codex", "a note for claude")
        answer = screen.chat("say", "--from", "claude", f"↪ #{mine} the answer")["id"]
        self.assertTrue(screen.until(lambda: "the answer" in screen.term.text()))
        def drafted(number):                # the screen trims trailing spaces, so match the number's end
            return re.search(rf"/reply #{number}(?!\d)", screen.term.text()) is not None

        screen.type("/rep")
        screen.type("\t")                  # the menu's Tab: /reply, and the newest message to the user
        self.assertTrue(screen.until(lambda: drafted(answer)))
        screen.type("\t")                  # again: the one before, skipping what wasn't for them
        self.assertTrue(screen.until(lambda: drafted(mention)))
        screen.type("\t")                  # and round to the newest
        self.assertTrue(screen.until(lambda: drafted(answer)))
        screen.type("\x7f" * (len(str(answer)) + 1) + f"{mention} thanks\r")
        self.assertTrue(screen.until(lambda: len([m for m in screen.messages() if m["sender"] == "user"]) == 2))
        self.assertEqual([m["text"] for m in screen.messages() if m["sender"] == "user"][-1], f"↪ #{mention} thanks")

    def test_shift_enter_starts_a_new_line_and_enter_alone_sends(self):
        shift = os.path.join(self.folder.name, "shift-held")
        screen = self.open(AGI_SHIFT_FILE=shift)
        screen.type("first line")
        Path(shift).touch()                 # Shift is down: this Enter is a new line
        screen.type("\r")
        os.remove(shift)
        screen.type("second line")
        screen.type("\x1b[13;2u")           # a terminal that sends Shift+Enter apart
        screen.type("third line")
        self.assertTrue(screen.until(lambda: "third line" in screen.term.text()))
        self.assertEqual(screen.messages(), [])
        screen.type("\r")                   # Enter alone sends all three lines as one message
        self.assertTrue(screen.until(lambda: screen.messages()))
        self.assertEqual([m["text"] for m in screen.messages()], ["first line\nsecond line\nthird line"])

    def test_two_screens_of_one_chat_pop_up_once_and_not_while_either_is_focused(self):
        record = os.path.join(self.folder.name, "notified.txt")
        fake = os.path.join(self.folder.name, "fake-notify")
        Path(fake).write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" >> {record}\necho >> {record}\n")
        os.chmod(fake, 0o755)
        first = self.open(AGI_NOTIFY_CMD=fake)
        second = ChatScreen(self.data, AGI_NOTIFY_CMD=fake)
        self.others.append(second)
        for screen in (first, second):
            self.assertTrue(screen.until(lambda: screen.term.text().strip()))     # drawn and listening
            screen.type("\x1b[O")         # both terminal windows lose focus
        first.chat("say", "--from", "codex", "@user one ping")
        self.assertTrue(first.until(lambda: "one ping" in first.term.text()) and
                        second.until(lambda: "one ping" in second.term.text()))
        first.pump(0.5)
        second.pump(0.5)
        self.assertEqual(Path(record).read_text().count("one ping"), 1)
        second.type("\x1b[I")             # the user looks at the other screen
        second.pump(0.3)
        first.chat("say", "--from", "claude", "@user while you look")
        self.assertTrue(first.until(lambda: "while you look" in first.term.text()))
        first.pump(0.5)
        self.assertNotIn("while you look", Path(record).read_text())

    def test_window_title_counts_messages_while_away(self):
        screen = self.open()
        screen.type("\x1b[O")
        screen.chat("say", "--from", "codex", "one")
        screen.chat("say", "--from", "claude", "two")
        self.assertTrue(screen.until(lambda: b"\x1b]0;AGI (2)\x07" in screen.raw))
        screen.type("\x1b[I")
        self.assertTrue(screen.until(lambda: screen.raw.rfind(b"\x1b]0;AGI\x07")
                                     > screen.raw.rfind(b"AGI (2)")))

    def test_close_messages_from_one_agent_share_a_header(self):
        screen = self.open()
        long = "a sentence that is long enough to fill the whole first row of the transcript at eighty columns"
        ids = [screen.chat("say", "--from", "codex", text)["id"] for text in ("first", long, "third " + long)]
        screen.chat("say", "--from", "claude", "different sender")
        self.assertTrue(screen.until(lambda: "different sender" in screen.term.text()))
        text = screen.term.text()
        self.assertEqual(text.count("● codex"), 1)
        self.assertEqual(text.count("● claude"), 1)
        for message_id in ids:                       # every message keeps its own number
            self.assertIn(f"#{message_id} ", text)

    def test_reply_quotes_its_target_without_borrowing_its_mentions(self):
        screen = self.open()
        original = screen.chat("say", "--from", "codex", "@user should we ship the store fix?")
        screen.until(lambda: "ship the store fix" in screen.term.text())
        screen.type(f"/reply {original['id']} yes, ship it\r")
        self.assertTrue(screen.until(lambda: screen.messages()[-1]["sender"] == "user"))
        self.assertEqual(screen.messages()[-1]["text"], f"↪ #{original['id']} yes, ship it")
        self.assertTrue(screen.until(lambda: f"↪ #{original['id']} codex: @user should we ship" in screen.term.text()))
        screen.chat("say", "--from", "claude", f"↪ #{original['id']} agreed")
        self.assertTrue(screen.until(lambda: "agreed" in screen.term.text()))
        self.assertEqual(screen.term.text().count("▌"), 1)        # only codex's original mentioned the user

    def test_oversized_reply_reference_is_plain_text(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "↪ #" + "9" * 5000 + " still readable")
        self.assertTrue(screen.until(lambda: "still readable" in screen.term.text()))
        self.assertIsNone(os.waitpid(screen.pid, os.WNOHANG)[0] or None)   # the screen is still running

    def test_touchpad_scrolls_the_chat_not_the_history(self):
        screen = self.open(60)
        screen.chat("say", "--from", "user", "an earlier message of mine")
        screen.until(lambda: "an earlier message of mine" in screen.term.text())
        self.assertIn(b"\x1b[?1006", screen.raw)              # mouse reporting is on
        screen.type("\x1b[<64;10;5M\x1b[<64;10;5M")          # two wheel-up steps
        self.assertTrue(screen.until(lambda: "Esc to jump" in screen.term.text()))
        self.assertNotIn("❯ an earlier message of mine", screen.term.text().splitlines()[-3])  # draft untouched
        for _ in range(4):
            screen.type("\x1b[<65;10;5M")                     # wheel down back to the bottom
        self.assertTrue(screen.until(lambda: "Esc to jump" not in screen.term.text()))

    def test_light_terminal_gets_the_light_palette(self):
        self.screen = screen = ChatScreen(self.data, background="ffff/ffff/ffff")
        screen.type("hello\r")
        self.assertTrue(screen.until(lambda: b"48;5;254" in screen.raw))   # light band behind your message
        self.assertIn(b"38;5;28", screen.raw)
        self.assertNotIn(b"48;5;236", screen.raw)

    def test_dark_terminal_gets_the_dark_palette(self):
        self.screen = screen = ChatScreen(self.data, background="1616/1616/1d1d")
        screen.type("hello\r")
        self.assertTrue(screen.until(lambda: b"48;5;236" in screen.raw))
        self.assertIn(b"38;5;157", screen.raw)

    def test_late_colour_reply_never_reaches_the_draft(self):
        screen = self.open()
        screen.type("\x1b]11;rgb:0000/0000/0000\x07")
        screen.type("x")
        self.assertTrue(screen.until(lambda: "❯ x" in screen.term.text()))
        self.assertNotIn("rgb:", screen.term.text())

    def test_mode_commands_update_the_footer_at_once(self):
        screen = self.open()
        footer = lambda: screen.term.text().splitlines()[-1]
        for command, expect in [("/custom 10 1 60", "custom · 0/10 per min until"), ("/pause", "paused ·"),
                                ("/resume", "custom · "), ("/custom off", "normal · 0/5 per min")]:
            screen.type(command + "\r")
            self.assertTrue(screen.until(lambda: expect in footer(), 5), f"{command}: {footer()}")
        screen.type("/custom 4 1\r")                                           # no end (#1823)
        self.assertTrue(screen.until(lambda: "custom · 0/4 per min" in footer(), 5), footer())
        self.assertNotIn("until", footer())

    def test_export_writes_a_markdown_range_and_who_counts_unread(self):
        exports = os.path.join(self.folder.name, "exports")
        screen = self.open(AGI_EXPORTS=exports)
        for text in ("first", "second", "third"):
            screen.chat("say", "--from", "codex", text)
        screen.until(lambda: "third" in screen.term.text())
        screen.type("/export 2-3\r")
        self.assertTrue(screen.until(lambda: "Saved " in screen.term.text() and "chat-2-3.md" in screen.term.text()))
        [name] = os.listdir(exports)
        text = Path(exports, name).read_text()
        self.assertIn("**#2 codex**", text)
        self.assertIn("third", text)
        self.assertNotIn("first", text)
        screen.type("/who\r")
        self.assertTrue(screen.until(lambda: "claude:" in screen.term.text() and "unread" in screen.term.text()))
        self.assertIn("claude: ", screen.term.text())
        self.assertIn("3 unread", screen.term.text())          # claude hasn't acked codex's three messages
        screen.type("x")
        self.assertTrue(screen.until(lambda: "unread" not in screen.term.text()))   # then go
        screen.type("\x7f/claims\r")
        self.assertTrue(screen.until(lambda: "No active claims" in screen.term.text()))
        screen.type("x")
        self.assertTrue(screen.until(lambda: "No active claims" not in screen.term.text()))

    def test_an_open_board_fills_in_and_follows_new_tasks(self):
        # Codex (#1643): opened before the first status pass, the board kept saying "Checking tasks…"
        screen = self.open()
        screen.type("/tasks\r")                       # at once: the status thread waits for a quiet moment
        self.assertTrue(screen.until(lambda: "Checking tasks" in screen.term.text()
                                     or "No requests taken yet" in screen.term.text()))
        self.assertTrue(screen.until(lambda: "No requests taken yet" in screen.term.text()))
        request = screen.chat("say", "--from", "user", "make the board")
        screen.chat("take", "--as", "claude", str(request["id"]))
        screen.chat("done", "--as", "claude", str(request["id"]), "--result", "in abc123", "--check", "abc123 gated",
                    "--skills", "test-runs")
        self.assertTrue(screen.until(lambda: f"#{request['id']} claude done ✓" in screen.term.text()
                                     and "in abc123" in screen.term.text()))

    def test_slash_opens_a_command_menu(self):
        screen = self.open()
        screen.type("/")
        self.assertTrue(screen.until(lambda: "/reply N text" in screen.term.text()
                                     and "answer message N" in screen.term.text()))
        screen.type("re")                   # narrows to /reply, /resume, /release
        self.assertTrue(screen.until(lambda: "/find" not in screen.term.text() and "/resume" in screen.term.text()))
        screen.type("\x1bOB")               # ↓ (keypad mode, as the terminal sends it) selects the second match
        screen.type("\t")                   # Tab fills it in
        # filled in with a trailing space (screen lines are compared right-trimmed), so the menu closes
        self.assertTrue(screen.until(lambda: "❯ /resume" in screen.term.text()
                                     and "let agents post again" not in screen.term.text()))
        screen.type("\x15/wh\r")            # Enter on a no-argument command runs it
        self.assertTrue(screen.until(lambda: "unread" in screen.term.text()))

    def test_command_search_finds_an_action_in_the_smallest_terminal(self):
        screen = self.open(rows=8, cols=30, AGI_THEME="light")
        screen.type("/search")
        self.assertTrue(screen.until(lambda: "› /find words" in screen.term.text()))
        screen.type("\r")
        self.assertTrue(screen.until(lambda: "❯ /find" in screen.term.text()
                                     and "› /find words" not in screen.term.text()))
        screen.type("\x15/stop\r")
        self.assertTrue(screen.until(lambda: "❯ /pause" in screen.term.text()))
        self.assertNotEqual(screen.chat("guard")["mode"], "paused")

    def test_argument_picker_fills_before_changing_policy(self):
        screen = self.open()
        screen.type("/mode w")
        self.assertTrue(screen.until(lambda: "› /mode wait" in screen.term.text()))
        screen.type("\r")
        self.assertTrue(screen.until(lambda: "❯ /mode wait" in screen.term.text()))
        self.assertEqual(screen.chat("guard")["mode"], "normal")
        screen.type("\r")
        self.assertTrue(screen.until(lambda: "your turn" in screen.term.text()))
        self.assertEqual(screen.chat("guard")["mode"], "wait")

    def test_bare_export_uses_the_whole_chat_with_one_enter(self):
        screen = self.open()
        screen.chat("say", "--from", "user", "a line to export")
        screen.type("/export\r")
        self.assertTrue(screen.until(lambda: "Saved " in screen.term.text()))
        (export,) = (Path(self.folder.name) / "exports").glob("*.md")
        self.assertIn("a line to export", export.read_text(encoding="utf-8"))

    def test_release_completion_keeps_spaces_and_unicode(self):
        screen = self.open()
        path = "work/長い notes.md"
        screen.chat("say", "--from", "user", "--kind", "claim", path)
        screen.type("/release work/長い")
        self.assertTrue(screen.until(lambda: f"› /release {path}" in screen.term.text()))
        screen.type("\t")
        self.assertTrue(screen.until(lambda: f"❯ /release {path}" in screen.term.text()))
        screen.type("\r")
        self.assertTrue(screen.until(lambda: f"released {path}" in screen.term.text()))

    def test_find_highlights_the_words_until_esc(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "the needle is here")
        screen.until(lambda: "the needle is here" in screen.term.text())
        screen.type("/find needle\r")
        self.assertTrue(screen.until(lambda: b"48;5;220" in screen.raw and "Found #1" in screen.term.text()))
        mark = len(screen.raw)
        screen.type("\x1b")
        self.assertTrue(screen.until(lambda: b"needle" in screen.raw[mark:]))   # redrawn without the yellow
        screen.pump(0.3)
        self.assertNotIn(b"48;5;220", screen.raw[mark:])

    def test_a_reply_to_the_user_counts_as_addressed_to_them(self):
        screen = self.open()
        mine = screen.chat("say", "--from", "user", "which theme should we use?")
        screen.chat("say", "--from", "codex", f"↪ #{mine['id']} the dark one")
        self.assertTrue(screen.until(lambda: "the dark one" in screen.term.text()
                                     and f"↪ #{mine['id']} you: which theme" in screen.term.text()
                                     and "▌ the dark one" in screen.term.text()))

    def journal_shim(self):
        """Paths of a gate file (blocks the journal write while present) and a fail file (makes it raise)."""
        shim = os.path.join(self.folder.name, "shim")
        os.makedirs(shim)
        Path(shim, "sitecustomize.py").write_text(JOURNAL_SHIM)
        gate, fail = os.path.join(shim, "gate"), os.path.join(shim, "fail")
        return dict(PYTHONPATH=shim, AGI_TEST_GATE=gate, AGI_TEST_FAIL=fail), gate, fail

    def test_a_stalled_send_keeps_the_screen_responsive(self):
        env, gate, _ = self.journal_shim()
        Path(gate).touch()                  # the disk "stalls" inside the pending-journal write
        screen = self.open(60, **env)
        screen.type("hello\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        self.assertLeftOnTheRule(screen, "sending…")
        start = time.time()
        screen.type("\x1b[5~")              # the screen still scrolls while the send is stuck
        self.assertTrue(screen.until(lambda: "Esc to jump" in screen.term.text(), 3))
        self.assertLess(time.time() - start, 1.0)
        self.assertFalse([m for m in screen.messages() if m["text"] == "hello"])
        os.remove(gate)                     # the disk recovers
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()].count("hello") == 1))
        self.assertTrue(screen.until(lambda: "sending…" not in screen.term.text()))
        screen.pump(0.5)
        self.assertEqual([m["text"] for m in screen.messages()].count("hello"), 1)   # exactly once

    def test_a_slow_send_leaves_no_ghost_draft(self):
        env, gate, _ = self.journal_shim()
        Path(gate).touch()
        screen = self.open(**env)
        screen.type("ghost check\r")       # Enter before the 1 s draft save has run
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.pump(2.0)                    # the save delay passes while the send is stuck
        os.remove(gate)
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()] == ["ghost check"]))
        self.assertTrue(screen.until(lambda: "sending…" not in screen.term.text()))
        screen.pump(1.5)                    # give any wrongly queued save time to land
        os.kill(screen.pid, signal.SIGKILL)
        os.waitpid(screen.pid, 0)
        self.screen = again = ChatScreen(self.data, **env)
        again.pump(1.5)
        self.assertNotIn("Restored your unsent draft", again.term.text())
        composer = again.term.text().splitlines()[-3]    # the input box line, not the transcript
        self.assertIn("Message claude and codex", composer)

    def test_text_typed_during_a_stalled_send_is_kept_for_the_next_message(self):
        # the UI bug hunt (2026-11-18): keys typed right after Enter were dropped while the send was in flight
        env, gate, _ = self.journal_shim()
        screen = self.open(**env)
        Path(gate).touch()
        screen.type("first\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.type("next one")
        screen.pump(0.3)
        os.remove(gate)
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()][-1:] == ["first"]))
        self.assertTrue(screen.until(lambda: "❯ next one" in screen.term.text()))

    def test_a_command_that_fails_says_why_and_keeps_the_draft(self):
        # the UI bug hunt (2026-11-18): /export errors vanished with the draft; /claim said "did you mean /claim?"
        screen = self.open()
        screen.type("/export 3-1\r")
        self.assertTrue(screen.until(lambda: "Usage: /export" in screen.term.text()))
        self.assertIn("❯ /export 3-1", screen.term.text())
        screen.type("\x15/claim\r\r")               # the menu fills it in; Enter again runs it
        self.assertTrue(screen.until(lambda: "Usage: /claim path/to/file" in screen.term.text()))
        self.assertNotIn("did you mean", screen.term.text())

    def test_a_failed_send_keeps_what_was_typed_meanwhile_without_sending_it(self):
        # Codex (#1431): a queued Enter replayed into the failed draft would send old and new text at once
        env, gate, _ = self.journal_shim()
        screen = self.open(**env)
        screen.chat("say", "--from", "user", "first")
        screen.until(lambda: "first" in screen.term.text())
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        Path(gate).touch()
        screen.type("lost?\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.type("next\rline")
        screen.pump(0.3)
        log.chmod(0o444)
        os.remove(gate)
        self.assertTrue(screen.until(lambda: "Not sent" in screen.term.text()))
        log.chmod(0o644)
        screen.pump(0.5)
        self.assertEqual([m["text"] for m in screen.messages()], ["first"])      # nothing went by itself
        self.assertIn("lost?", screen.term.text())
        self.assertIn("line", screen.term.text())
        self.assertNotIn("lost?next", screen.term.text())   # replayed, the Enter would have retried at once

    def test_a_paste_whose_end_never_comes_lets_go_after_a_pause(self):
        # the UI bug hunt (2026-11-18): without the end marker every later key, Ctrl+C too, joined the paste
        screen = self.open()
        screen.type("\x1b[200~half a paste")
        screen.pump(2.4)
        screen.type(" and typing")
        self.assertTrue(screen.until(lambda: "❯ half a paste and typing" in screen.term.text()))

    def test_find_shows_the_match_with_its_sender_even_in_a_folded_message(self):
        # the UI bug hunt (2026-11-18): the header row was cut off, and a match in a fold stayed hidden
        screen = self.open()
        long = "\n".join(f"line {n}" for n in range(1, 31)) + "\nthe NEEDLE is on line 31"
        screen.chat("say", "--from", "codex", long)
        for n in range(8):
            screen.chat("say", "--from", "user", f"filler {n}")
        screen.until(lambda: "filler 7" in screen.term.text())
        screen.type("/find needle\r")
        self.assertTrue(screen.until(lambda: "NEEDLE" in screen.term.text()))
        screen.type("\x1b")
        screen.chat("say", "--from", "codex", "the unique zebra token")
        for n in range(12):                 # enough below it that the view has to scroll to show it at the top
            screen.chat("say", "--from", "user", f"more {n}")
        screen.until(lambda: "more 11" in screen.term.text())
        screen.type("/find zebra\r")
        self.assertTrue(screen.until(lambda: "Found #" in screen.term.text()))
        rows = [row.split("│", 1)[-1] for row in screen.term.text().splitlines()]   # right of the rail's line
        top = next(n for n, row in enumerate(rows) if row.strip())
        self.assertIn("codex", rows[top])                           # its sender's row, not cut off

    def test_reading_older_messages_stays_put_when_new_ones_arrive(self):
        screen = self.open()
        for n in range(40):
            screen.chat("say", "--from", "user", f"message {n}")
        screen.until(lambda: "message 39" in screen.term.text())
        screen.type("\x1b[5~")
        self.assertTrue(screen.until(lambda: "Esc to jump" in screen.term.text()))
        screen.pump(0.3)
        shown = [row for row in screen.term.text().splitlines() if "message" in row]
        screen.chat("say", "--from", "user", "a new one\nwith a second line")
        screen.pump(1.0)
        self.assertEqual([row for row in screen.term.text().splitlines() if "message" in row], shown)

    def test_history_recall_waits_while_a_send_is_in_flight(self):
        env, gate, _ = self.journal_shim()
        screen = self.open(**env)
        screen.chat("say", "--from", "user", "an older message")
        screen.until(lambda: "an older message" in screen.term.text())
        Path(gate).touch()
        screen.type("new one\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.type("\x1bOA\t\x1b\x1b")      # ↑ recall, Tab, double Esc: none may change the draft now
        screen.pump(0.5)
        self.assertIn("❯ new one", screen.term.text().splitlines()[-3])
        os.remove(gate)
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()][-1] == "new one"))
        self.assertTrue(screen.until(lambda: "Message claude and codex" in screen.term.text().splitlines()[-3]))

    def test_quitting_during_a_stuck_send_waits_then_exits(self):
        env, gate, _ = self.journal_shim()
        Path(gate).touch()
        screen = self.open(**env)
        screen.type("keep me safe\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.type("\x03\x03", 0)
        self.assertTrue(screen.until(lambda: "still saving your message" in screen.term.text()))
        screen.pump(16)                     # past the old 15 s silent exit (Codex reproduced that loss)
        self.assertEqual(os.waitpid(screen.pid, os.WNOHANG)[0], 0, "quit before the send was saved")
        self.assertIn("still saving your message", screen.term.text())
        os.remove(gate)                     # the disk recovers: the screen finishes and quits by itself
        self.assertEqual(screen.quit_wait(), 0)
        self.assertEqual([m["text"] for m in screen.messages()], ["keep me safe"])

    def test_forcing_quit_during_a_stuck_send_prints_the_unsent_text(self):
        env, gate, _ = self.journal_shim()
        Path(gate).touch()
        screen = self.open(**env)
        big = "\n".join(f"pasted line {n} " + "y" * 50 for n in range(40))
        screen.type("do not lose this ")
        screen.type("\x1b[200~" + big + "\x1b[201~")
        self.assertTrue(screen.until(lambda: "[Pasted text #1" in screen.term.text()))
        screen.type("\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.type("\x03\x03", 0)
        self.assertTrue(screen.until(lambda: "still saving your message" in screen.term.text()))
        screen.type("\x03", 0)              # quit anyway
        self.assertEqual(screen.quit_wait(), 0)
        tail = screen.raw.decode("utf-8", "replace")
        self.assertIn("NOT sent or saved", tail)
        printed = tail.split("NOT sent or saved")[-1]
        self.assertIn("do not lose this", printed)
        self.assertIn("pasted line 39", printed)          # the paste itself, not its chip label
        self.assertNotIn("[Pasted text #1", printed)
        os.remove(gate)

    def test_signals_during_a_stuck_send_print_the_unsent_text(self):
        env, gate, _ = self.journal_shim()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            Path(gate).touch()
            self.screen = screen = ChatScreen(self.data, **env)
            screen.type(f"text before {sig.name}\r")
            self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
            os.kill(screen.pid, sig)
            self.assertEqual(screen.quit_wait(), 0, f"{sig.name}: did not exit promptly")
            printed = screen.raw.decode("utf-8", "replace").split("NOT sent or saved")[-1]
            self.assertIn(f"text before {sig.name}", printed, sig.name)
            os.remove(gate)
            os.close(screen.fd)
            self.screen = None

    def test_fresh_text_survives_quit_and_is_printed_on_a_signal(self):
        screen = self.open()
        screen.type("fresh words")          # quit before the 1 s draft save
        screen.type("\x03\x03", 0)
        self.assertEqual(screen.quit_wait(), 0)
        os.close(screen.fd)
        self.screen = again = ChatScreen(self.data)
        self.assertTrue(again.until(lambda: "Restored your unsent draft" in again.term.text()
                                    and "fresh words" in again.term.text()))
        again.type("\x15more fresh words")
        os.kill(again.pid, signal.SIGTERM)  # a signal before the save: printed, never written in place
        self.assertEqual(again.quit_wait(), 0)
        self.assertIn("more fresh words", again.raw.decode("utf-8", "replace").split("NOT sent or saved")[-1])

    def test_a_failed_final_save_keeps_the_screen_open(self):
        env, _, fail = self.journal_shim()
        Path(fail + "-saves").touch()
        screen = self.open(**env)
        screen.type("unsaved thought")
        screen.type("\x03\x03", 0)         # quit before the debounce: the final save is queued and fails
        self.assertTrue(screen.until(lambda: "Draft not saved: injected save failure" in screen.term.text()))
        screen.pump(1.0)
        self.assertEqual(os.waitpid(screen.pid, os.WNOHANG)[0], 0, "quit with unsaved text")
        self.assertIn("❯ unsaved thought", screen.term.text())
        os.remove(fail + "-saves")
        screen.type("\x03\x03", 0)         # retry: now it saves and quits
        self.assertEqual(screen.quit_wait(), 0)
        os.close(screen.fd)
        self.screen = again = ChatScreen(self.data, **env)
        self.assertTrue(again.until(lambda: "unsaved thought" in again.term.text()
                                    and "Restored your unsent draft" in again.term.text()))

    def test_a_failed_journal_write_keeps_the_draft_for_retry(self):
        env, _, fail = self.journal_shim()
        Path(fail).touch()
        screen = self.open(**env)
        screen.type("retry me\r")
        self.assertTrue(screen.until(lambda: "Not sent: injected journal failure" in screen.term.text()
                                     and "❯ retry me" in screen.term.text()))
        self.assertEqual(screen.messages(), [])
        os.remove(fail)
        screen.type("\r")
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()] == ["retry me"]))

    def test_clocks_tick_without_typing(self):
        screen = self.open()
        screen.chat("status", "--as", "claude", "working")
        screen.chat("custom", "1", "1", "5")     # 1 agent message per minute: the second post is refused
        screen.chat("say", "--from", "codex", "first")
        screen.chat("say", "--from", "codex", "second")
        self.assertTrue(screen.until(lambda: "working" in screen.term.text() and "agents wait" in screen.term.text()))
        def seconds(pattern):
            found = re.search(pattern, screen.term.text())   # the rail's "working" has its time on the next row
            return int(found.group(1)) if found else None
        working = r"working[^\n]*\n\s*(?:#\d+ · )?(\d+)s"
        first_work, first_wait = seconds(working), seconds(r"agents wait (\d+)s")
        start = len(screen.raw)
        screen.pump(3.2)                    # no keys pressed
        ticks = screen.raw[start:]          # each tick is one frame, the cursor hidden while it moves to the clock
        begin, end = b"\x1b[?2026h\x1b[?25l", b"\x1b[?25h\x1b[?2026l"
        first, last = ticks.find(begin), ticks.rfind(end)
        self.assertTrue(0 <= first < last)  # at least one whole frame
        whole = ticks[first:last + len(end)]   # the window may start or stop inside a frame being written
        self.assertEqual(whole.count(begin), whole.count(end))
        self.assertTrue(not ticks[:first] or ticks[:first].endswith(end))       # the tail of an earlier frame
        self.assertTrue(not ticks[last + len(end):] or ticks[last + len(end):].startswith(begin[:1]))
        self.assertGreaterEqual(seconds(working), first_work + 2)
        self.assertLessEqual(seconds(r"agents wait (\d+)s"), first_wait - 2)

    def test_paused_and_wait_modes_stay_idle(self):
        screen = self.open()
        for mode, shown in (("/pause", "paused ·"), ("/mode wait", "your turn")):
            screen.type(mode + "\r")
            self.assertTrue(screen.until(lambda: shown in screen.term.text()))
            screen.pump(0.3)
            ticks = lambda: sum(map(int, Path(f"/proc/{screen.pid}/stat").read_text().split()[13:15]))
            before = ticks()
            screen.pump(2.0)                # no keys: a refresh loop would burn ~200 ticks (2 s of CPU) here
            self.assertLess(ticks() - before, 20, f"{mode}: busy while idle")

    def assertLeftOnTheRule(self, screen, text):
        line = next(line for line in screen.term.text().splitlines() if text in line)
        chat = line.split("│", 1)[-1]                   # right of the rail, when it shows
        self.assertTrue(chat.startswith("─") and chat.index(text) <= 3, line)

    def test_two_press_hints_expire_without_typing(self):
        screen = self.open()
        screen.type("\x03")                 # one Ctrl+C
        self.assertTrue(screen.until(lambda: "ctrl+c again to quit" in screen.term.text()))
        self.assertLeftOnTheRule(screen, "ctrl+c again to quit")
        self.assertTrue(screen.until(lambda: "ctrl+c again to quit" not in screen.term.text(), 5))

    def test_quit_restores_the_terminal(self):
        screen = self.open()
        screen.chat("say", "--from", "claude", "a message to keep")
        screen.until(lambda: "a message to keep" in screen.term.text())
        self.assertEqual(screen.quit(), 0)
        tail = screen.raw.decode("utf-8", "replace")
        self.assertIn("\x1b[?2004l", tail)
        self.assertIn("a message to keep", tail.split("AGI · last messages")[-1])





    def admin_ack(self, message_id):
        admin_ack(self.data, message_id)

    @staticmethod
    def row(screen, message_id):
        return next((line for line in screen.term.text().splitlines() if re.search(rf"#{message_id} \d\d:\d\d$", line)), None)

    @staticmethod
    def marks(screen):
        """Every mark glyph on the message rows (the row above the box has its own ○)."""
        return "".join(re.findall("[○✓✔☑]", "".join(line for line in screen.term.text().splitlines()
                                                    if re.search(r"#\d+ \d\d:\d\d$", line))))

    def thread(self, **options):
        screen = self.open(**options)
        screen.chat("say", "--from", "user", "@codex and @admin look at this")      # 1
        screen.chat("say", "--from", "claude", "on it")                              # 2
        screen.chat("say", "--from", "codex", "@claude review this")                 # 3
        self.assertTrue(screen.until(lambda: self.row(screen, 3)))
        return screen

    def test_a_user_message_gets_a_dot_per_agent_when_delivered_then_checks_when_acked(self):
        screen = self.thread()
        screen.pump(2.5)                         # past a status pass: nothing delivered, nothing shown
        self.assertEqual(self.marks(screen), "")
        # each step waits for a status pass (2 s): the gate's 180 s budget keeps them to the ones that differ
        woke(self.data, "claude", 1)
        woke(self.data, "codex", 1)
        self.assertTrue(screen.until(lambda: "○ ○ #1" in (self.row(screen, 1) or "")))   # a dot each, delivered
        screen.chat("ack", "--as", "claude", "1")
        self.assertTrue(screen.until(lambda: "✓ ○ #1" in (self.row(screen, 1) or "")))   # one check beside a dot
        screen.chat("ack", "--as", "codex", "1")
        self.admin_ack(1)                        # it says @admin: the admin's check joins, last
        self.assertTrue(screen.until(lambda: "✓ ✓ ✓ #1" in (self.row(screen, 1) or "")))

    def test_marks_stay_on_older_messages_and_agent_chatter_gets_none(self):
        screen = self.thread()
        woke(self.data, "claude", 3, first=1)
        woke(self.data, "codex", 2, first=1)
        screen.chat("ack", "--as", "claude", "3")
        screen.chat("ack", "--as", "codex", "3")
        self.assertTrue(screen.until(lambda: "✓ ✓ #1" in (self.row(screen, 1) or "")))
        self.assertTrue(screen.until(lambda: "✓ #3" in (self.row(screen, 3) or "")))     # codex's @claude: claude only
        self.assertEqual(self.row(screen, 3).count("✓"), 1)
        self.assertTrue(re.search(r"#2 \d\d:\d\d$", self.row(screen, 2)) and not re.search("[○✓✔☑]", self.row(screen, 2)))

    def test_marks_are_coloured_per_participant_and_carry_no_names(self):
        screen = self.thread()
        screen.chat("ack", "--as", "codex", "1")
        self.admin_ack(1)
        self.assertTrue(screen.until(lambda: "✓ ✓ #1" in (self.row(screen, 1) or "")))
        for name in ("✓claude", "✓codex", "✓admin", "✓ claude"):
            self.assertNotIn(name, screen.term.text())
        import re as regex
        # admin's, the one before it codex's
        last = screen.raw.rindex("✓".encode())
        for at, colour in ((last, b"38;5;178"), (screen.raw.rindex("✓".encode(), 0, last), b"38;5;69")):   # codex: brand blue (#5579)
            recent = b" ".join(regex.findall(rb"\x1b\[([0-9;]*)m", screen.raw[:at])[-3:])
            self.assertIn(colour, recent, at)

    def test_messages_not_delivered_get_no_marks(self):
        screen = self.open()
        screen.chat("say", "--from", "claude", "@codex @admin your turn")
        self.assertTrue(screen.until(lambda: self.row(screen, 1)))
        screen.pump(2.5)                         # past a status pass
        self.assertEqual(self.marks(screen), "")


def private_display(test, server_too=False):
    """A private Xvfb display for a test, isolated from the active desktop; it stops with the test, and
    the clipboard's owners with it. Skips where there's no Xvfb or no GTK for Python. `server_too`: also the
    server's process, for a test that stops it early."""
    import shutil
    import importlib.util
    if not shutil.which("Xvfb") or importlib.util.find_spec("gi") is None:
        test.skipTest("needs Xvfb and python3-gi")
    server = subprocess.Popen(["Xvfb", "-displayfd", "1", "-nolisten", "tcp", "-screen", "0", "320x200x24"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    test.addCleanup(lambda: (server.kill(), server.wait(), server.stdout.close()))
    if not select.select([server.stdout], [], [], 15)[0]:
        test.fail("Xvfb didn't start")
    display = ":" + server.stdout.readline().decode().strip()
    return (display, server) if server_too else display


def ask_clipboard(display, target):
    """What another program (GTK, in its own process) gets asking the clipboard for `target`: the bytes, or for
    TARGETS the names listed; None when the owner refuses."""
    script = (
        "import sys, gi\n"
        "gi.require_version('Gtk', '3.0')\n"
        "from gi.repository import Gdk, Gtk\n"
        "board = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)\n"
        "if sys.argv[1] == 'TARGETS':\n"
        "    found, targets = board.wait_for_targets()\n"
        "    sys.stdout.buffer.write(' '.join(t.name() for t in targets).encode() if found else b'')\n"
        "else:\n"
        "    got = board.wait_for_contents(Gdk.Atom.intern(sys.argv[1], False))\n"
        "    sys.stdout.buffer.write(b'+' + got.get_data() if got is not None and got.get_length() >= 0 else b'')\n")
    done = subprocess.run([sys.executable, "-I", "-c", script, target], capture_output=True, timeout=30,
                          env={**os.environ, "DISPLAY": display, "GDK_BACKEND": "x11", "NO_AT_BRIDGE": "1"})
    if target == "TARGETS":
        return done.stdout.decode().split() or None
    return done.stdout[1:] if done.stdout else None


def resident_kb(pid):
    with open(f"/proc/{pid}/status") as status:
        return int(next(line for line in status if line.startswith("VmRSS:")).split()[1])


class ClipboardTests(ScreenCase):
    """Copying keeps working after the copy, with no clipboard manager running."""

    def test_the_owner_keeps_the_text_until_something_else_is_copied(self):
        from unittest import mock
        from agi.ui import clipboard
        display = private_display(self)
        with mock.patch.dict(os.environ, {"DISPLAY": display}):
            first = clipboard.copy("as written ✓\n  indented")
            self.assertEqual(clipboard.read(), ("text", "as written ✓\n  indented"))   # copy() itself has returned
            second = clipboard.copy("newer")
            self.assertTrue(waited(lambda: not os.path.exists(f"/proc/{first}"), 10), "the old owner stayed")
            self.assertEqual(clipboard.read(), ("text", "newer"))
            self.assertTrue(os.path.exists(f"/proc/{second}"))
        with mock.patch.dict(os.environ, {"DISPLAY": ":"}):
            with self.assertRaisesRegex(ValueError, "no desktop display"):
                clipboard.copy("nowhere")

    def test_any_text_round_trips_in_every_form_it_offers(self):
        from unittest import mock
        from agi.ui import clipboard
        display = private_display(self)
        words = "emoji 🙂🎉, 日本語, café\n\ttabbed ✓"
        with mock.patch.dict(os.environ, {"DISPLAY": display}):
            clipboard.copy(words)
            self.assertEqual(clipboard.read(), ("text", words))      # through GTK, as Ctrl+V reads it
        listed = ask_clipboard(display, "TARGETS")
        for name in ("TARGETS", "UTF8_STRING", "text/plain;charset=utf-8", "STRING", "TEXT"):
            self.assertIn(name, listed)
        self.assertEqual(ask_clipboard(display, "UTF8_STRING"), words.encode())
        self.assertEqual(ask_clipboard(display, "text/plain;charset=utf-8"), words.encode())
        self.assertEqual(ask_clipboard(display, "STRING"), "emoji ??, ???, café\n\ttabbed ?".encode("latin-1"))
        self.assertEqual(ask_clipboard(display, "TEXT"), words.encode())            # not all Latin-1: UTF-8
        self.assertIsNone(ask_clipboard(display, "image/png"))                      # refused, not made up
        with mock.patch.dict(os.environ, {"DISPLAY": display}):
            clipboard.copy("café")
        self.assertEqual(ask_clipboard(display, "TEXT"), "café".encode("latin-1"))  # all Latin-1: STRING

    def test_the_owner_ends_when_another_program_copies_or_the_display_goes(self):
        from unittest import mock
        from agi.ui import clipboard
        display, server = private_display(self, server_too=True)
        with mock.patch.dict(os.environ, {"DISPLAY": display}):
            owner = clipboard.copy("ours")
        taker = ("import gi\ngi.require_version('Gtk', '3.0')\nfrom gi.repository import Gdk, Gtk\n"
                 "board = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)\nboard.set_text('theirs', -1)\n"
                 "print(board.wait_for_text(), flush=True)\n")
        took = subprocess.run([sys.executable, "-I", "-c", taker], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "DISPLAY": display, "GDK_BACKEND": "x11", "NO_AT_BRIDGE": "1"})
        self.assertEqual(took.stdout.strip(), "theirs")
        # well within the owner's own 10 s look: the server's SelectionClear ended it
        self.assertTrue(waited(lambda: not os.path.exists(f"/proc/{owner}"), 5), "the owner stayed")
        with mock.patch.dict(os.environ, {"DISPLAY": display}):
            owner = clipboard.copy("ours again")
        server.kill()                                         # logout: the connection closes under the owner
        self.assertTrue(waited(lambda: not os.path.exists(f"/proc/{owner}"), 5), "the owner outlived the display")
        with mock.patch.dict(os.environ):
            os.environ.pop("DISPLAY", None)
            with self.assertRaisesRegex(ValueError, "no desktop display to copy to .DISPLAY isn't set"):
                clipboard.copy("nowhere")

    def test_the_owner_stays_small(self):
        from unittest import mock
        from agi.ui import clipboard
        if not os.path.exists(f"/proc/{os.getpid()}/status"):
            self.skipTest("needs /proc")
        display = private_display(self)
        with mock.patch.dict(os.environ, {"DISPLAY": display}):
            owner = clipboard.copy("a few words ✓")
            self.assertEqual(clipboard.read(), ("text", "a few words ✓"))  # it has served a request, too
        self.assertLess(resident_kb(owner), 20 * 1024)

    def test_text_bigger_than_one_request_is_refused_and_the_clipboard_kept(self):
        from unittest import mock
        from agi.ui import clipboard
        display = private_display(self)
        with mock.patch.dict(os.environ, {"DISPLAY": display}):
            owner = clipboard.copy("kept")
            with self.assertRaisesRegex(ValueError, "^too much text to copy at once$"):
                clipboard.copy("x" * (16 << 20))              # Xvfb's BIG-REQUESTS limit is 16 MB less 4 bytes
            self.assertEqual(clipboard.read(), ("text", "kept"))
            self.assertTrue(os.path.exists(f"/proc/{owner}"))

    def test_the_event_structs_match_xlib_h(self):
        import ctypes
        from agi.ui import clipboard_owner as owner
        if ctypes.sizeof(ctypes.c_long) != 8:
            self.skipTest("the offsets below are for 64-bit Linux")
        # from libx11-dev 2:1.8.12's Xlib.h: int type; unsigned long serial; Bool send_event; Display *display;
        # then XIDs, Atoms and Times, each an unsigned long
        self.assertEqual(ctypes.sizeof(owner.XEvent), 24 * 8)
        for struct, fields, size in (
                (owner.SelectionRequestEvent, ("owner", "requestor", "selection", "target", "property", "time"), 80),
                (owner.SelectionEvent, ("requestor", "selection", "target", "property", "time"), 72),
                (owner.SelectionClearEvent, ("window", "selection", "time"), 56),
                (owner.PropertyEvent, ("window", "atom", "time", "state"), 64)):
            self.assertEqual([getattr(struct, name).offset for name in ("type", "serial", "send_event", "display")],
                             [0, 8, 16, 24])
            self.assertEqual([getattr(struct, name).offset for name in fields], list(range(32, 32 + 8 * len(fields), 8)))
            self.assertEqual(ctypes.sizeof(struct), size)

    def test_a_drag_copies_with_ctrl_c_and_the_text_outlives_the_screen(self):
        from unittest import mock
        from agi.ui import clipboard
        display = private_display(self)
        screen = self.open(rows=20, cols=60, DISPLAY=display)
        screen.chat("say", "--from", "codex", "the zebra words wrap across the rows of this narrow screen, as long "
                                              "messages do\n- second zebra line")
        self.assertTrue(screen.until(lambda: "second zebra line" in screen.term.text()))
        # motion while a button is held (a drag), asked for after curses' own request for clicks, which replaces it
        self.assertIn(b"\x1b[?1006;1000h", screen.raw)
        self.assertGreater(screen.raw.find(b"\x1b[?1002h"), screen.raw.find(b"\x1b[?1006;1000h"))
        lines = screen.term.text().splitlines()
        top = next(n for n, line in enumerate(lines) if "the zebra" in line)
        bottom = next(n for n, line in enumerate(lines) if "second zebra line" in line)
        x = lines[top].index("the zebra")
        mark = len(screen.raw)
        for event in (f"0;{x + 1};{top + 1}M", f"32;{x + 5};{top + 2}M", f"32;50;{bottom + 1}M", f"0;50;{bottom + 1}m"):
            screen.type(f"\x1b[<{event}", 0.1)    # press, drag down past the line's end, release: one at a time, as a
                                                   # hand makes them (read together, curses drops the press)
        self.assertTrue(screen.until(lambda: re.search(rb"\x1b\[0(;\d+)*;7(;\d+)*m", screen.raw[mark:])))  # reversed
        self.assertNotIn("/reply", screen.term.text())            # a drag is no click
        screen.type("\x03")
        self.assertTrue(screen.until(lambda: "Copied 2 lines" in screen.term.text(), 20))
        self.assertNotIn("ctrl+c again", screen.term.text())
        os.kill(screen.pid, signal.SIGKILL)                       # the screen is gone; the text isn't
        os.waitpid(screen.pid, 0)
        expected = "the zebra words wrap across the rows of this narrow screen, as long messages do\n- second zebra line"
        with mock.patch.dict(os.environ, {"DISPLAY": display}):
            self.assertEqual(clipboard.read(), ("text", expected))
        other = ChatScreen(os.path.join(self.folder.name, "other"), rows=20, cols=60, DISPLAY=display)
        self.others.append(other)
        other.type("\x16")                                         # Ctrl+V: the text, into the draft
        self.assertTrue(other.until(lambda: "second zebra line" in other.term.text(), 20))
        other.type("\r")
        self.assertTrue(other.until(lambda: other.messages()))
        self.assertEqual(other.messages()[-1]["text"], expected)


def waited(condition, seconds):
    end = time.time() + seconds
    while time.time() < end and not condition():
        time.sleep(0.05)
    return condition()


class LatencyTests(ScreenCase):
    """Timing-sensitive: run alone, not in parallel with other suites.

    Functional:  python3 -I tests/test_ui.py --functional   (every class except this one)
    Latency:     python3 -I tests/test_ui.py --latency
    """

    def test_keystrokes_stay_fast_with_long_history(self):
        screen = self.open(3000)
        delays = []
        for ch in "quick brown fox":
            before = len(screen.raw)
            start = time.time()
            os.write(screen.fd, ch.encode())
            while len(screen.raw) == before and time.time() - start < 2:
                screen.pump(0.002)
            delays.append(time.time() - start)
            screen.pump(0.02)
        self.assertLess(statistics.median(delays), 0.05, delays)


def _selected(latency, shard=None):
    """Every test class in this module: only LatencyTests, or everything else. New classes join automatically.
    `shard` (index, count): every count-th test of the sorted ids from index on, so shards 1..count together run
    each test exactly once (#5054: the functional suite split across the runner's groups)."""
    loader, tests = unittest.TestLoader(), []
    for case in globals().values():
        if isinstance(case, type) and issubclass(case, unittest.TestCase) and case is not ScreenCase:
            if issubclass(case, LatencyTests) == latency:
                tests.extend(loader.loadTestsFromTestCase(case))
    if shard:
        index, count = shard
        tests = sorted(tests, key=lambda test: test.id())[index - 1::count]
    return unittest.TestSuite(tests)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] in (["--functional"], ["--latency"]):
        result = unittest.TextTestRunner().run(_selected(sys.argv[1] == "--latency"))
        sys.exit(0 if result.wasSuccessful() else 1)
    if len(sys.argv) == 4 and sys.argv[1:3] == ["--functional", "--shard"]:        # --shard 1/2
        index, _, count = sys.argv[3].partition("/")
        if not (index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count)):
            sys.exit("--shard takes N/M, 1 <= N <= M")
        result = unittest.TextTestRunner().run(_selected(False, (int(index), int(count))))
        sys.exit(0 if result.wasSuccessful() else 1)
    unittest.main()                         # no arguments: everything
