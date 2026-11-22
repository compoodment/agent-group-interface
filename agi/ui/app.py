"""The main loop: watcher thread, poll loop, resize, hangup and clean exit. Owner: Claude."""

import curses
import functools
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
from pathlib import Path

from agi import admin_view, gates, membership, reminders, store, ui_state
from agi.ui import keys as modified
from agi.ui.composer import DRAG_OFF
from agi.ui.screen import STATUS_EVERY, Screen, Writer
from agi.ui.text import clean, fit


WATCH_TIMEOUT = 300         # seconds per wait_for_change call; only bounds thread wakeups
MODIFIED_KEYS = os.environ.get("AGI_KEYS") != "legacy"   # ask terminals to report Shift+Enter and friends


# -- background watcher -----------------------------------------------------

def with_membership(presence):
    """Presence with each agent's chat membership beside it (#6005): joined, left by choice, or another session
    asking to join. A membership file that can't be read shows as nothing extra, never as a crash."""
    for agent in store.AGENTS:
        try:
            presence.setdefault(agent, {})["member"] = membership.status(agent)
        except Exception:
            pass
    return presence


class Watcher(threading.Thread):
    """Waits for messages newer than `seen` and writes a byte to `wake_fd`."""

    def __init__(self, seen, wake_fd):
        super().__init__(daemon=True)
        self.seen = seen
        self.wake_fd = wake_fd
        self.consumed = threading.Event()
        self.error = None

    def run(self):
        while True:
            try:
                changed = store.wait_for_change(self.seen, WATCH_TIMEOUT)
            except Exception as error:      # surfaced on screen, then retried
                self.error = error
                changed = True
                time.sleep(5)
            if changed:
                self.consumed.clear()
                os.write(self.wake_fd, b".")
                self.consumed.wait()

    def advance(self, seen):
        self.seen = seen
        self.consumed.set()



def read_acks():
    """Each participant's ack cursor, for the read marks. One that can't be read is left out, never an error."""
    acks = {}
    for name, read in (*((agent, functools.partial(store.get_ack, agent)) for agent in store.AGENTS),
                       ("admin", admin_view.get_ack)):
        try:
            value = read()
        except (OSError, ValueError, RecursionError):
            continue
        if value is not None:
            acks[name] = value
    return acks


def read_wakes():
    """Each agent's delivery receipts as (first id, last id) ranges, for the read marks' hollow dots. The store keeps
    the 20 most recent per agent in its cursor file; one that can't be read is left out, never an error."""
    wakes = {}
    for agent in store.AGENTS:
        try:
            wakes[agent] = store.wake_ranges(agent)
        except (OSError, ValueError, RecursionError):
            continue
    return wakes


class StatusThread(threading.Thread):
    """Loads guard, claims, presence and the task board off the input thread, every few seconds or when asked."""

    QUIET = 1.5                             # seconds without a key before the cold first scan
    QUIET_AT_MOST = 4.0                     # how long the first scan waits for that, at most

    def __init__(self, wanted, wake_fd, last_key_at, gates_visible):
        super().__init__(daemon=True)
        self.wanted = wanted
        self.wake_fd = wake_fd
        self.last_key_at = last_key_at      # callable: monotonic time of the latest keypress
        self.result = None
        self.gates_visible = gates_visible
        self.gate_result = None

    def run(self):
        # The first pass parses the whole log in pure Python and competes with typing for the GIL
        # (~100-200 ms echo on 5k messages). Wait for a quiet moment; warm passes later take ~4 ms.
        started = time.monotonic()         # ... but not for ever: scrolling or typing on kept it "checking…" (sweep)
        while time.monotonic() - self.last_key_at() < self.QUIET and time.monotonic() - started < self.QUIET_AT_MOST:
            time.sleep(0.25)
        while True:
            try:
                reminders.wait_limit(STATUS_EVERY)
                self.result = (store.guard_status(), store.active_claims(), with_membership(store.get_presence()),
                               store.last_id(),
                               store.list_tasks(),   # the whole log's requests: the transcript keeps only a tail
                               store.list_todos(include_done=True),   # the rail's count, /todo, and a clicked T#
                                                                      # that's done (#4092, #4257)
                               read_acks(), read_wakes())
            except Exception as error:      # shown on screen by apply_status
                self.result = error
            if self.gates_visible():
                try:
                    # tests: never the live gates of the repository they run beside, which changed under them
                    # whenever one of our merges ran (a rail that "moved" in a flicker test, #5537)
                    live = os.environ.get("AGI_GATES") != "0"
                    self.gate_result = gates.snapshot(store._main_root()) if live else []
                except (OSError, ValueError):
                    self.gate_result = None
            os.write(self.wake_fd, b"s")
            self.wanted.wait(STATUS_EVERY)
            self.wanted.clear()


def _run(win, screen_box):
    curses.raw()
    curses.nonl()
    curses.curs_set(1)
    curses.set_escdelay(25)
    win.nodelay(True)
    win.keypad(True)
    curses.mouseinterval(0)                 # wheel steps arrive at once, no click-detection delay
    # paste mode, focus reports, title; and modified-key reports, so Shift+Enter can be told from Enter
    sys.stdout.write("\x1b[?2004h\x1b[?1004h\x1b]0;AGI\x07" + (modified.ENABLE if MODIFIED_KEYS else ""))
    sys.stdout.flush()
    screen = Screen(win)
    screen_box.append(screen)
    screen.refresh_data(initial=True)
    try:                                    # the saved /settings; AGI_MOUSE and AGI_NOTIFY still override them
        settings = ui_state.user_settings()
    except (OSError, ValueError):
        settings = {"mouse": os.environ.get("AGI_MOUSE") != "0", "notifications": True}
    screen.set_mouse(settings["mouse"])
    screen.notify_on = settings["notifications"]
    screen.open_state()
    if os.environ.get("AGI_SETUP_NOTICE") != "0":   # tests: only the tests about it see it
        screen.show_setup(quiet=True)       # a chat no agent has joined yet says how to set one up (#6027)
    try:
        position = _open_reading_position(screen)
        _loop(win, screen)
        if position is not None and not screen.force_quit and screen.row_owners:
            historical = screen.view is not None or screen.scroll
            anchor = screen.row_owners[0] if historical else None
            if anchor != position.anchor:
                screen.writer.submit("position", position.save, anchor)
    finally:
        screen.close_state()


def _open_reading_position(screen):
    try:
        position = ui_state.ReadingPosition()
    except OSError:
        return None                    # a temporary UI preference never prevents opening the chat
    anchor = position.anchor
    if anchor is not None:
        try:
            if anchor not in screen.by_id:
                screen.view = store.read_messages(max(0, anchor - 15), limit=40)
            if not screen.jump_to(anchor):
                screen.view = None
        except (OSError, ValueError):
            screen.view = None
    return position


def _pipe():
    """A non-blocking pipe that background threads and signal handlers use to wake the main loop."""
    read, write = os.pipe()
    os.set_blocking(read, False)
    os.set_blocking(write, False)
    return read, write


def _drain(fd):
    try:
        os.read(fd, 4096)
    except BlockingIOError:
        pass


def _loop(win, screen):
    wake_read, wake_write = _pipe()
    resize_read, resize_write = _pipe()
    signal.signal(signal.SIGWINCH, lambda signum, frame: os.write(resize_write, b"r"))
    watcher = Watcher(screen.last_id, wake_write)
    watcher.start()
    status_read, status_write = _pipe()
    writes_read, writes_write = _pipe()
    screen.writer = Writer(writes_write)
    screen.writer.start()
    status = StatusThread(screen.status_wanted, status_write, lambda: screen.last_key_at,
                          lambda: screen.gates_visible() or screen.rail_shown)
    status.start()
    screen.draw()                           # first paint: messages only; status arrives from its thread
    stdin = sys.stdin.fileno()
    poller = select.poll()
    for fd in (stdin, wake_read, resize_read, status_read, writes_read):
        poller.register(fd, select.POLLIN)
    hangup = select.POLLHUP | select.POLLERR | select.POLLNVAL
    while True:
        edited = screen.edited_at           # a draft waiting to be saved wakes the loop when it's due, not up to a
        due = 1000 if edited is None else int((edited + 1 - time.monotonic()) * 1000) + 5   # second late: a
        try:                                # power cut then loses about a second of typing at most (playtest, #1941)
            events = poller.poll(max(10, min(1000, due)))
        except InterruptedError:
            events = []
        if any(fd == stdin and flags & hangup for fd, flags in events):
            return                          # the terminal is gone: exit instead of spinning
        ready = [fd for fd, flags in events if flags & select.POLLIN]
        dirty = False
        if resize_read in ready:
            _drain(resize_read)
            try:
                size = os.get_terminal_size(sys.stdout.fileno())
                curses.resizeterm(size.lines, size.columns)
            except (OSError, curses.error):  # a terminal can briefly report 0x0 mid-resize: draw at the old size
                pass
            win.clearok(True)               # a full repaint, inside one frame: clearing now flashed a blank screen
            screen.draw()                   # before the next key read, whose implicit refresh would repaint first
        if wake_read in ready:
            _drain(wake_read)
            if watcher.error:
                screen.note(f"Store error: {watcher.error}", "error")
                watcher.error = None
            try:
                screen.refresh_data()
            except (ValueError, OSError) as error:
                screen.note(f"Store error: {error}", "error")
            watcher.advance(screen.last_id)
            dirty = True
        screen.read_keys()
        if screen.pending:
            screen.last_key_at = time.monotonic()
            dirty = True
            if not screen.handle_pending():
                screen.capture_seen()       # the quit key itself is an interaction
                if not screen.force_quit:
                    screen.queue_final_writes()
                if screen.force_quit or not screen.writes_pending():
                    return
                screen.closing = True       # a send or save is still on the disk: wait for it, visibly
                screen.notes = [("still saving your message… · Ctrl+C again to quit anyway", "error", None)]
                dirty = True
        if screen.closing:
            dirty |= screen.apply_writes()  # see a failed save first: it cancels closing
            if screen.closing and not screen.writes_pending():
                return                      # the writer finished cleanly: now it's safe to quit
        screen.capture_seen()
        screen.flush_seen()
        screen.save_draft()
        if writes_read in ready:
            _drain(writes_read)
            if screen.apply_writes():
                dirty = True
        dirty |= screen.take_background()    # a finished /update or /backup step
        if status_read in ready:
            _drain(status_read)
            dirty |= screen.apply_status(status.result)
            dirty |= screen.apply_gates(status.gate_result)
        if screen.notes and any(expiry and expiry <= time.monotonic() for _, _, expiry in screen.notes):
            dirty = True
        title = f"AGI ({screen.away})" if screen.away else "AGI"
        if title != screen.title:           # the window title counts messages that arrived while away
            screen.title = title
            sys.stdout.write(f"\x1b]0;{title}\x07")
            sys.stdout.flush()
        if screen.retry_deadline and screen.retry_left() == 0:
            screen.retry_deadline = None    # the countdown ran out: ask for the fresh status once
            screen.request_status()
        # Redraw on change, every second while a seconds-level clock shows, else once a minute.
        tick = int(time.time()) if screen.ticking() else int(time.time()) // 60
        if dirty or tick != screen.drawn_minute:
            screen.drawn_minute = tick
            screen.draw()


def _goodbye(screen):
    """Leave the last messages in the terminal's scrollback, and any text that wasn't saved."""
    if screen.unsaved:
        print("AGI · NOT sent or saved (the disk was still busy when you quit). Your text:")
        print(screen.unsaved)
        print()
    recent = [m for m in list(screen.messages)[-12:] if m["kind"] == "msg"]
    if not recent:
        return
    print("AGI · last messages")
    for message in recent:
        name = "you" if message["sender"] == store.USER else message["sender"]
        text = " ".join(clean(message["text"]).split())
        print(f"  #{message['id']} {name}: {fit(text, 100)}")
    print("Reopen with: agi")


def detect_theme(timeout=0.25):
    """Ask the terminal for its background colour (OSC 11). 'light', 'dark', or None when it doesn't answer."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b]11;?\x07")
        sys.stdout.flush()
        reply, deadline = b"", time.monotonic() + timeout
        while time.monotonic() < deadline and not reply.endswith((b"\x07", b"\x1b\\")):
            if select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                reply += os.read(fd, 64)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    match = re.search(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", reply)
    if not match:
        return None
    red, green, blue = (int(part, 16) / (16 ** len(part) - 1) for part in match.groups())
    return "light" if 0.2126 * red + 0.7152 * green + 0.0722 * blue > 0.5 else "dark"


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("chat: the chat screen needs a terminal", file=sys.stderr)
        return 1
    if "AGI_THEME" not in os.environ and "NO_COLOR" not in os.environ:
        os.environ["AGI_THEME"] = detect_theme() or "dark"

    def stop(signum, frame):
        if screen_box:                      # a signal means "go now": don't wait on a stuck disk
            screen_box[0].force_quit = True
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    screen_box = []
    try:
        curses.wrapper(_run, screen_box)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?2004l\x1b[?1004l" + DRAG_OFF + (modified.DISABLE if MODIFIED_KEYS else ""))
        sys.stdout.flush()
        if screen_box:                      # also on SIGTERM/SIGHUP, whose SystemExit skips code after this block
            _goodbye(screen_box[0])
    launcher = screen_box[0].restart_with if screen_box else None
    if launcher and not screen_box[0].unsaved:   # /update: the new version, in this terminal, on the same chat
        # without this version's folder on the path: the new launcher puts its own first, and a module the new
        # version dropped mustn't quietly load from the old one (the /update rehearsal, T6168)
        old = str(Path(__file__).resolve().parents[2])
        path = [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                if entry and os.path.realpath(entry) != old]
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join(path))
        if not path:
            environment.pop("PYTHONPATH")
        os.execve(launcher, [launcher], environment)
    return 0
