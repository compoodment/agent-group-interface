"""Layout tests for text.py, render.py and desktop.py: narrow and short terminals, indents, popups.

Owner: Claude. Stdlib only; screen tests drive bin/agi in a pty with throwaway data (see tests/test_ui.py).
Run: python3 -m unittest tests.test_ui_layout
"""

import json
import re
import os
from pathlib import Path
import stat
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agi.ui import composer, desktop, render, text  # noqa: E402
from tests.test_ui import ScreenCase, Terminal, admin_ack, seed, woke  # noqa: E402


def flat(rows):
    return ["".join(part for part, _ in row) for row in rows]


class WrapIndentTests(unittest.TestCase):
    def test_leading_indent_of_a_line_is_kept(self):
        rows = text.wrap([("  ", 0)], "  ", [("  • nested", 0), ("\n", 0), ("    deeper", 0)], 40)
        self.assertEqual(flat(rows), ["    • nested", "      deeper"])

    def test_indent_is_kept_after_the_first_segment_and_in_a_user_row(self):
        rows = text.wrap([("❯ ", 0)], "  ", [("   a", 0)], 40)
        self.assertEqual(flat(rows), ["❯    a"])

    def test_whitespace_at_a_wrap_point_is_still_dropped(self):
        rows = text.wrap([("  ", 0)], "  ", [("aaaa bbbb cccc", 0)], 11)
        self.assertEqual(flat(rows), ["  aaaa bbbb", "  cccc"])


class WidthTests(unittest.TestCase):
    def test_skin_tone_modifier_adds_no_cells(self):
        self.assertEqual(text.text_width("👍"), 2)
        self.assertEqual(text.text_width("👍🏽"), 2)
        self.assertEqual(text.text_width("a👍🏽b"), 4)


class HintTests(unittest.TestCase):
    def test_hint_picks_the_longest_that_fits_and_cuts_the_last_resort(self):
        pick = render.Renderer._hint
        self.assertEqual(pick(30, " History view · Esc returns to latest ", " History · Esc "), " History · Esc ")
        self.assertEqual(pick(60, " History view · Esc returns to latest ", " History · Esc "),
                         " History view · Esc returns to latest ")
        self.assertEqual(text.text_width(pick(10, " long long long ", " also quite long ")), 10)

    def test_share_keeps_every_part_visible(self):
        parts = [("✻ claude working · 12s · a very long presence note indeed", 1), ("○ codex away", 2)]
        out = render.Renderer._share(parts, 40)
        self.assertEqual(out[1][0], "○ codex away")
        self.assertTrue(out[0][0].endswith("…"))
        self.assertLessEqual(sum(text.text_width(t) for t, _ in out) + 3, 40)
        self.assertEqual(render.Renderer._share(parts[1:], 40), parts[1:])


class NotifyTests(ScreenCase):
    def test_title_and_body_follow_a_double_dash(self):
        record = os.path.join(self.folder.name, "argv.json")
        tool = os.path.join(self.folder.name, "fake-notify")
        with open(tool, "w") as script:
            script.write(f"#!/usr/bin/env python3\nimport json, sys\njson.dump(sys.argv[1:], open({record!r}, 'w'))\n")
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IXUSR)
        old = {key: os.environ.get(key) for key in ("AGI_NOTIFY_CMD", "AGI_NOTIFY")}
        os.environ["AGI_NOTIFY_CMD"] = tool
        os.environ.pop("AGI_NOTIFY", None)
        from unittest import mock
        try:
            with mock.patch.object(desktop.store, "DATA_DIR", Path(self.data)):   # this chat's names, not the live one's
                shown = desktop.Desktop()
            shown._someone_focused = lambda: False
            self.assertTrue(shown.notify("claude", "-rf looks like an option"))
            for _ in range(100):
                if os.path.exists(record) and os.path.getsize(record):
                    break
                time.sleep(0.05)
            with open(record) as done:
                argv = json.load(done)
        finally:
            if shown.notifier:
                shown.notifier.close()
            for key, value in old.items():
                os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
        self.assertEqual(argv[-3:], ["--", "claude", "-rf looks like an option"])


class NarrowScreenTests(ScreenCase):
    def test_scroll_hint_has_its_own_row_and_stays_still_while_scrolling(self):
        screen = self.open(200)
        wheel_up = "\x1b[<64;10;5M"
        screen.type(wheel_up)
        self.assertTrue(screen.until(lambda: "Esc" in screen.term.text()))
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "Esc" in line)
        self.assertEqual(lines[row].split("▼")[0].split("│")[-1].strip(), "")   # nothing of a message under it (the rail beside it)
        start = len(screen.raw)
        screen.type(wheel_up)
        screen.pump(0.4)
        replay = Terminal(len(screen.term.grid), len(screen.term.grid[0]))
        replay.feed(screen.raw[:start].decode(errors="replace"))
        hint = replay.text().splitlines()[row]
        for ch in screen.raw[start:].decode(errors="replace"):   # every state the terminal passes through
            replay.feed(ch)
            self.assertEqual(replay.text().splitlines()[row], hint, "the hint row changed mid-scroll (flicker)")

    def test_the_rail_stays_still_while_the_chat_scrolls(self):
        # scrolling moved the rail's rows with the chat's, then they were drawn back
        # (the sweep after it: the same when the box, the / list or a note grows under the chat)
        screen = self.open(200, rows=24, cols=100)
        self.assertTrue(screen.until(lambda: "people" in screen.term.text() and "checking" not in screen.term.text()))
        screen.pump(0.5)                    # the status is in: from here the rail has no reason to change
        for keys in ("\x1b[<64;60;5M", "abc", "\n", "x" * 80, "\x15\x15/", "\x1b?"):   # wheel, a line more,
            start = len(screen.raw)                                                  # a wrap, the / list, ?
            screen.type(keys)
            screen.pump(0.4)
            replay = Terminal(len(screen.term.grid), len(screen.term.grid[0]))
            replay.feed(screen.raw[:start].decode(errors="replace"))
            rail = [line[:render.RAIL_WIDTH + 1] for line in replay.text().splitlines()]
            for ch in screen.raw[start:].decode(errors="replace"):   # every state the terminal passes through
                replay.feed(ch)
                now = [line[:render.RAIL_WIDTH + 1] for line in replay.text().splitlines()]
                self.assertEqual(now, rail, f"the rail moved after {keys!r} (flicker)")

    def test_a_fast_flick_keeps_every_wheel_step(self):
        # the sweep after #3342: ncurses keeps eight mouse events; read after draining the keys, a flick of 200
        # steps scrolled five messages
        screen = self.open(400, rows=24, cols=60)
        self.assertTrue(screen.until(lambda: "#400" in screen.term.text()))
        screen.type("\x1b[<64;30;5M" * 40)                        # forty steps up, in one read
        self.assertTrue(screen.until(lambda: "Esc" in screen.term.text()))
        screen.pump(0.5)
        shown = [int(n) for n in re.findall(r"#(\d+) \d\d:\d\d", screen.term.text())]
        self.assertLess(max(shown), 400 - 15, shown)               # 120 rows up (about 29 messages), not 24 (6)

    def test_scrolled_hint_with_new_arrivals_is_cut_not_dropped(self):
        screen = self.open(60, cols=30)
        screen.type("\x1b[5~")
        self.assertTrue(screen.until(lambda: "▼" in screen.term.text()))
        screen.chat("say", "--from", "codex", "arrived")
        self.assertTrue(screen.until(lambda: "new" in screen.term.text() and "▼ 1" in screen.term.text()))
        self.assertIn("Esc", next(line for line in screen.term.text().splitlines() if "▼ 1" in line))

    def test_the_new_count_goes_down_as_new_messages_scroll_into_view(self):
        # A newly visible message is removed from the unread count.
        screen = self.open(60, rows=24, cols=80)
        self.assertTrue(screen.until(lambda: "#60" in screen.term.text()))
        for _ in range(3):
            screen.type("\x1b[5~")                                 # well up into older messages
        self.assertTrue(screen.until(lambda: "▼" in screen.term.text()))
        for n in range(3):
            screen.chat("say", "--from", "codex", f"arrival {n}")
        self.assertTrue(screen.until(lambda: "▼ 3 new" in screen.term.text()))
        def count():
            match = re.search(r"▼ (\d+) new", screen.term.text())
            return int(match.group(1)) if match else 0
        seen = []
        for _ in range(12):                                         # down a few rows at a time
            screen.type("\x1b[<65;30;5M")
            screen.pump(0.15)
            seen.append(count())
            if "arrival 2" in screen.term.text():
                break
        self.assertIn("arrival 0", screen.term.text())
        self.assertEqual(seen, sorted(seen, reverse=True), seen)    # only ever down
        self.assertTrue(any(0 < value < 3 for value in seen), seen) # and live: part-way, part counted
        self.assertTrue(screen.until(lambda: "new" not in next(
            (line for line in screen.term.text().splitlines() if "▼" in line), "")))   # all on screen: none left

    def test_the_shortcuts_hint_shows_once_at_any_width(self):
        for cols, where in ((100, -1), (34, None)):
            screen = self.open(0, rows=20, cols=cols)
            self.assertTrue(screen.until(lambda: "Message clau" in screen.term.text()))
            screen.pump(0.3)
            lines = screen.term.text().splitlines()
            self.assertEqual(sum("? for shortcuts" in line for line in lines), 1, (cols, lines))
            if where is not None:                                       # wide: on the bottom line
                self.assertIn("? for shortcuts", lines[where])
            screen.quit()

    def test_history_hint_is_cut_not_dropped(self):
        seed(self.data, 1200)
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        lines = log.read_text().splitlines()
        record = json.loads(lines[4])
        record["text"] = "ancient needle here"
        lines[4] = json.dumps(record)
        log.write_text("\n".join(lines) + "\n")
        screen = self.open(cols=30)              # the full hint is 38 cells: it once vanished here
        screen.type("/find ancient needle\r")
        self.assertTrue(screen.until(lambda: "History" in screen.term.text()))
        self.assertIn("Esc", next(line for line in screen.term.text().splitlines() if "History" in line))

    def test_both_agents_stay_on_the_activity_row(self):
        screen = self.open(5, cols=40)
        screen.chat("status", "--as", "claude", "working", "--note", "on something with a very long presence note")
        screen.chat("status", "--as", "codex", "idle")
        def both():                         # the activity row, two above the input box (whose hint names both)
            lines = screen.term.text().splitlines()
            box = max((n for n, line in enumerate(lines) if line.startswith("❯")), default=None)   # your messages too
            return box is not None and box >= 2 and "claude" in lines[box - 2] and "codex" in lines[box - 2]
        self.assertTrue(screen.until(both))


class ShortScreenTests(ScreenCase):
    def test_menu_selection_and_activity_stay_visible_on_8_rows(self):
        screen = self.open(5, rows=8)
        names = list(composer.COMMAND_HELP)
        screen.type("/")
        self.assertTrue(screen.until(lambda: names[0] in screen.term.text()))
        for _ in range(4):
            screen.type("\x1b[B")
        self.assertTrue(screen.until(lambda: names[4] in screen.term.text()))
        lines = screen.term.text().splitlines()
        self.assertEqual(len(lines), 8)
        self.assertTrue(any("claude" in line or "checking" in line for line in lines))
        top = lines[0].strip()
        self.assertTrue(top and top not in "abcde" and set(top) != {"─"}, f"a transcript row remains: {top!r}")

    def test_five_row_composer_leaves_a_transcript_row(self):
        screen = self.open(5, rows=8)
        for word in "abcd":
            screen.type(word + "\n")        # Ctrl+J is a new line
        screen.type("e")
        self.assertTrue(screen.until(lambda: "  e" in screen.term.text().splitlines()[-3]))
        lines = screen.term.text().splitlines()
        self.assertEqual(len(lines), 8)
        top = lines[0].strip()
        self.assertTrue(top and top not in "abcde" and set(top) != {"─"}, f"a transcript row remains: {top!r}")


class TranscriptTests(ScreenCase):
    """Test activity rows, breaks between messages, the reply button, jumping to quotes."""

    def scene(self):
        screen = self.open(0, rows=30, cols=78)
        request = screen.chat("say", "--from", "user", "@claude please look")
        screen.chat("say", "--from", "claude", "first thought")
        screen.chat("say", "--from", "claude", "second thought")
        screen.chat("say", "--from", "codex", "--kind", "claim", "agi/store.py")
        screen.chat("take", "--as", "claude", str(request["id"]))
        screen.chat("say", "--from", "codex", "↪ #2 agreed with that")
        self.assertTrue(screen.until(lambda: "agreed with that" in screen.term.text()))
        return screen

    def click(self, screen, y, x):
        screen.type(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m")   # SGR press and release, 1-based

    def test_activity_rows_and_breaks(self):
        lines = self.scene().term.text().splitlines()
        self.assertIn("⚑ codex claimed agi/store.py", "\n".join(lines))
        self.assertIn("└  claude is on it", "\n".join(lines))          # still under the request it answers
        second = next(n for n, line in enumerate(lines) if "second thought" in line)
        self.assertEqual(lines[second - 1].strip(), "")              # the same agent again: a blank line
        first = next(n for n, line in enumerate(lines) if line.startswith("● claude"))
        self.assertEqual(lines[first - 1].strip(), "")               # the speaker changed: a blank line, a name

    def test_coloured_words_in_your_messages_keep_the_band_behind_them(self):
        screen = self.open(0, rows=20, cols=78)
        screen.chat("say", "--from", "user", "ping @claude and `zxqcode` here")   # words drawn nowhere else
        self.assertTrue(screen.until(lambda: "ping @claude" in screen.term.text()))
        screen.pump(0.5)
        import re
        for word in (b"@claude", b"zxqcode"):        # the escape right before each coloured word
            before = screen.raw[:screen.raw.rindex(word)]
            sgr = re.findall(rb"\x1b\[([0-9;]*)m", before)[-1]
            self.assertIn(b"48;5;236", sgr, word)    # a black hole in the band (#1166)

    def test_clicking_a_task_on_the_rail_jumps_to_its_request(self):
        self.prefill([("user", "@claude the zebra request"), ("claude", "claim", "task:1")]
                     + [("user", f"filler {n}") for n in range(30)])
        screen = self.open(0, rows=30, cols=100)
        request = {"id": 1}
        self.assertTrue(screen.until(lambda: "filler 29" in screen.term.text() and "the zebra request" in
                                     "".join(line[:render.RAIL_WIDTH] for line in screen.term.text().splitlines())))
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if line.startswith(f"#{request['id']} claude"))
        self.click(screen, row, 2)
        self.assertTrue(screen.until(lambda: any("❯ @claude the zebra request" in line
                                                 for line in screen.term.text().splitlines())))

    def test_clicking_an_id_starts_a_reply_and_a_quote_jumps(self):
        screen = self.scene()
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "#6 " in line)
        self.click(screen, row, len(lines[row]) - 2)
        self.assertTrue(screen.until(lambda: "❯ /reply #6" in screen.term.text()))  # on its own line (#3272); lines are trimmed
        screen.chat("custom", "60", "1", "5")
        for n in range(25):                                          # push #2 off the screen
            screen.chat("say", "--from", "user", f"filler {n}")
        self.assertTrue(screen.until(lambda: "filler 24" in screen.term.text()))
        self.assertNotIn("first thought", screen.term.text())
        for _ in range(8):                                           # page up until the quote shows
            if screen.until(lambda: "↪ #2 claude" in screen.term.text(), 1):
                break
            screen.type("\x1b[5~")
        self.assertIn("↪ #2 claude", screen.term.text())
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "↪ #2 claude" in line)
        self.click(screen, row, 4)
        self.assertTrue(screen.until(lambda: "first thought" in screen.term.text()
                                     and "#2 · Tab goes back" in screen.term.text()))

    def test_any_message_number_jumps_and_tab_comes_back(self):
        first = 1
        self.prefill([("claude", "the zebra decision")] + [("user", f"filler {n}") for n in range(25)]
                     + [("codex", f"as we said in #{first}, keep it")])
        screen = self.open(0, rows=20, cols=70)
        self.assertTrue(screen.until(lambda: "keep it" in screen.term.text()))
        self.assertNotIn("zebra", screen.term.text())
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if f"#{first}," in line)
        self.click(screen, row, lines[row].index(f"#{first},") + 1)
        self.assertTrue(screen.until(lambda: "the zebra decision" in screen.term.text()))
        screen.type("half typed")                                   # #3506: with words in the box too
        screen.type("\t")                                           # back where we were: the latest
        self.assertTrue(screen.until(lambda: "keep it" in screen.term.text() and "Back where you were"
                                     in screen.term.text()))
        self.assertNotIn("zebra", screen.term.text())
        self.assertIn("❯ half typed", screen.term.text())             # the words stay

    def prefill(self, records):
        """(sender, text) or (sender, kind, text) records, written into the throwaway log before the screen opens:
        far faster than a CLI call each (the layout suite has a 90 s budget on the Mini)."""
        folder = Path(self.data) / "chat"
        folder.mkdir(parents=True)
        day = time.strftime("%Y-%m-%d")
        with (folder / f"{day}.jsonl").open("w") as log:
            for number, record in enumerate(records, 1):
                sender, kind, said = record if len(record) == 3 else (record[0], "msg", record[1])
                log.write(json.dumps({"id": number, "ts": f"{day}T09:{number // 60:02d}:{number % 60:02d}+01:00",
                                      "sender": sender, "kind": kind, "text": said}) + "\n")
        (Path(self.data) / "seq").write_text(f"{len(records)}\n")

    def test_admin_posts_like_an_agent(self):
        self.prefill([("user", "@admin did the test service restart?"), ("admin", "the service is back and checked"),
                      ("admin", "and backups are on"), ("claude", "thanks")])
        screen = self.open(0, rows=24, cols=70)
        self.assertTrue(screen.until(lambda: "backups are on" in screen.term.text()))
        lines = screen.term.text().splitlines()
        self.assertTrue(any(line.startswith("● admin") for line in lines), lines)   # a name header, not a "·" line
        self.assertEqual(sum(line.startswith("● admin") for line in lines), 1)       # two in a row share it
        self.assertFalse(any("· the service is back and checked" in line for line in lines))

    def test_a_reply_to_admin_reaches_admin_and_carries_its_mark(self):
        self.prefill([("user", "@admin all good there?"), ("admin", "yes, the restart went fine")])
        screen = self.open(0, rows=20, cols=70)
        self.assertTrue(screen.until(lambda: "restart went fine" in screen.term.text()))
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if line.startswith("● admin"))
        self.click(screen, row, len(lines[row]) - 2)
        self.assertTrue(screen.until(lambda: "❯ /reply #2" in screen.term.text()))
        self.assertFalse(any("@admin" in line for line in screen.term.text().splitlines()[-4:]))   # nothing added
        screen.type("thanks!\r")
        self.assertTrue(screen.until(lambda: any(m["text"] == "↪ #2 thanks!" for m in screen.messages())))
        sent = next(m for m in screen.messages() if m["text"] == "↪ #2 thanks!")
        self.assertEqual(sent.get("reply_to"), 2)                     # the store marks it a reply
        admin_ack(self.data, sent["id"])                              # admin can take it: it reached admin
        self.assertTrue(screen.until(lambda: "✓" in (TranscriptTests.row_of(screen, sent["id"]) or "")))

    @staticmethod
    def row_of(screen, message_id):
        return next((line for line in screen.term.text().splitlines() if re.search(rf"#{message_id} \d\d:\d\d$", line)), None)

    def test_tasks_moves_the_keyboard_into_the_rail(self):
        self.prefill([("user", "@claude the zebra request"), ("claude", "claim", "task:1"),
                      ("user", "@codex the yak request"), ("codex", "claim", "task:3")]
                     + [("user", f"filler {n}") for n in range(30)])
        screen = self.open(0, rows=30, cols=100)
        self.assertTrue(screen.until(lambda: "filler 29" in screen.term.text() and "yak request" in screen.term.text()))
        screen.type("/tasks\r")
        self.assertTrue(screen.until(lambda: "Enter opens it" in screen.term.text()))
        screen.type("\x1b[B")                                      # down: from the newest (#3) to #1
        screen.type("\r")
        self.assertTrue(screen.until(lambda: any("❯ @claude the zebra request" in line
                                                 for line in screen.term.text().splitlines())))
        screen.type("\x1b")
        self.assertTrue(screen.until(lambda: "Enter opens it" not in screen.term.text()))
        screen.type("hello")                                        # typing goes to the box again
        self.assertTrue(screen.until(lambda: "❯ hello" in screen.term.text()))

    def test_sending_keeps_your_place(self):
        self.prefill([("claude", "an older zebra thought")] + [("user", f"filler {n}") for n in range(25)])
        screen = self.open(0, rows=20, cols=70)
        self.assertTrue(screen.until(lambda: "filler 24" in screen.term.text()))
        for _ in range(10):                                          # page up to the older one
            if screen.until(lambda: "zebra" in screen.term.text(), 0.5):
                break
            screen.type("\x1b[5~")
        self.assertIn("zebra", screen.term.text())
        screen.type("my answer\r")
        self.assertTrue(screen.until(lambda: any(m["text"] == "my answer" for m in screen.messages())))
        # the box clears once the send is confirmed, later on a loaded runner than a fixed 0.5 s (the gate, #5537)
        self.assertTrue(screen.until(lambda: "❯ my answer" not in screen.term.text()))
        self.assertIn("zebra", screen.term.text())                    # still reading there


class LinkTests(ScreenCase):
    """Links in messages should be clickable and open."""

    def test_a_link_shows_its_words_and_a_click_opens_it(self):
        import os, stat, tempfile
        root = tempfile.mkdtemp(dir=os.path.dirname(self.data))
        report, opened = os.path.join(root, "audit.md"), os.path.join(root, "opened")
        with open(report, "w") as file:
            file.write("# audit\n")
        opener = os.path.join(root, "opener")
        with open(opener, "w") as file:      # records what it was asked to open; never a real viewer
            file.write(f"#!/bin/sh\necho \"$1\" >> {opened}\n")
        os.chmod(opener, stat.S_IRWXU)
        screen = self.open(0, rows=20, cols=60, AGI_OPEN=opener, AGI_LINK_ROOT=root)
        screen.chat("say", "--from", "codex", f"See the [Audit and measurements]({report}) and [a script]({opener}).")
        self.assertTrue(screen.until(lambda: "Audit and measurements ↗" in screen.term.text()))
        self.assertNotIn(report, screen.term.text())                  # the words, not the long path
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "measurements" in line)
        self.click(screen, row, lines[row].index("measurements") + 2)
        self.assertTrue(screen.until(lambda: os.path.exists(opened)))
        with open(opened) as file:
            self.assertEqual(file.read().split(), [report])
        second = os.path.join(root, "second.md")                     # Codex (#1488): two links with the same
        with open(second, "w") as file:                               # words, and one quoted in code
            file.write("# second\n")
        screen.chat("say", "--from", "codex", f"`[Audit]({report})` then [Audit]({report}) then [Audit]({second})")
        self.assertTrue(screen.until(lambda: screen.term.text().count("Audit ↗") == 2))   # the quoted one isn't
        lines = screen.term.text().splitlines()                                         # a link
        row = [n for n, line in enumerate(lines) if "Audit ↗" in line][-1]              # the last, wherever it wraps
        self.click(screen, row, lines[row].rindex("Audit") + 1)
        self.assertTrue(screen.until(lambda: Path(opened).read_text().split() == [report, second]))
        screen.chat("say", "--from", "codex", "the [page](https://example.com/a/very/long/path/4) itself")
        self.assertTrue(screen.until(lambda: "page ↗" in screen.term.text()))
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "page ↗" in line)
        self.click(screen, row, lines[row].index("page ↗") + 1)
        self.assertTrue(screen.until(lambda: "Opening example.com" in screen.term.text()))   # its site, not "4"
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "script ↗" in line)   # the link's own words: a hint
        self.click(screen, row, lines[row].index("script ↗") + 1)              # can wrap "a" to the line above
        self.assertTrue(screen.until(lambda: "Not opened" in screen.term.text()))   # a program never runs
        with open(opened) as file:
            self.assertEqual(file.read().split(), [report, second, "https://example.com/a/very/long/path/4"])

    click = TranscriptTests.click


class ActivityTests(unittest.TestCase):
    def test_the_task_in_hand_is_the_newest_with_older_ones_counted(self):
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.attr = {"claude": 1, "codex": 2, "dim": 3}
        screen.presence = {"claude": {"state": "working", "since": time.time(), "reachable": True},
                           "codex": {"state": "idle", "reachable": True}}
        screen.claims = {"task:715": "claude", "task:1139": "claude", "task:1059": "claude", "task:1090": "codex",
                         "docs/backlog.md": "claude"}
        claude, codex = (text for text, _ in screen.activity(80))
        self.assertTrue(claude.startswith("✻ claude working · #1139 +2"), claude)   # it said "#715" (#1139)
        self.assertEqual(codex, "○ codex waiting · #1090")          # holds a task, no turn running (#1500)

    def test_each_status_word_says_what_is_true(self):
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.attr = {"claude": 1, "codex": 2, "dim": 3}
        screen.claims = {"task:7": "codex"}
        screen.presence = {"claude": {"state": "idle", "reachable": True},
                           "codex": {"state": "unknown", "reachable": False}}
        claude, codex = (text for text, _ in screen.activity(80))
        self.assertEqual(claude, "○ claude idle")                    # reachable, nothing held
        self.assertEqual(codex, "○ codex away · #7")                 # not reachable; its task stays visible


class RailTests(unittest.TestCase):
    """The rail's tasks."""

    class Win:
        def __init__(self):
            self.cells = {}

        def getmaxyx(self):
            return 40, 100

        def addstr(self, y, x, text, attr=0):
            self.cells[y] = self.cells.get(y, "").ljust(x)[:x] + text

    def rail(self, tasks, height, todos=None):
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.attr = {"claude": 1, "codex": 2, "dim": 3, "rule": 4, "user": 5}
        screen.presence, screen.claims, screen.acks, screen.gate_snapshot = {}, {}, {}, None
        screen.task_list, screen.todo_list = tasks, todos
        screen.win = self.Win()
        screen.draw_rail(height)
        return [screen.win.cells.get(y, "")[:render.RAIL_WIDTH].rstrip() for y in range(height)]

    @staticmethod
    def task(number, state="claimed", owner="claude", text="@claude @codex: fix the export dialog"):
        return {"id": number, "state": state, "owner": owner, "text": text, "event_ts": "2026-11-20T12:00:00+01:00"}

    def test_newest_first_without_the_names_in_front(self):
        rows = self.rail([self.task(9, owner="codex"), self.task(5), self.task(3, "done")], 20)
        start = rows.index("tasks") + 1
        self.assertEqual([row.split(" ")[0] for row in rows[start:start + 6:2]], ["#9", "#5", "#3"])
        self.assertEqual(rows[start + 1], "  fix the export dialog")    # not "  @claude @codex: fix…"

    def test_a_full_rail_says_how_many_more(self):
        rows = self.rail([self.task(n) for n in range(20, 10, -1)], 12)
        self.assertIn("#20 claude", rows[rows.index("tasks") + 1])     # the newest stays
        self.assertTrue(any(row.startswith("+") and row.endswith("more · /tasks") for row in rows), rows)

    def test_todo_shows_what_you_can_do_now_then_the_rest(self):
        todo = lambda actor: {"next_actor": actor}
        rows = self.rail([self.task(5)], 20, [todo("user"), todo("claude"), todo(None)])
        self.assertEqual(rows[rows.index("you 1 · /todo") + 1], "later 2 · /todo")
        rows = self.rail([self.task(5)], 20, [todo("codex")])
        self.assertFalse(any(row.startswith("you") for row in rows), rows)
        self.assertIn("later 1 · /todo", rows)
        self.assertFalse(any("/todo" in row for row in self.rail([self.task(5)], 20, [])))   # nothing: no lines

    def test_a_task_shows_its_helpers(self):
        # #4168: "#N claude +codex" in the rail
        task = self.task(9, owner="claude")
        task["helpers"] = ["codex"]
        rows = self.rail([task], 20)
        self.assertTrue(rows[rows.index("tasks") + 1].startswith("#9 claude +codex "), rows)

    def test_nothing_in_hand_says_so(self):
        self.assertIn("none in hand", self.rail([], 12))

    def test_a_clicked_task_opens_all_its_words_and_jumps_to_it(self):
        from agi import ui
        long = "@claude fix the export dialog so it remembers the folder, the format and the size across restarts"
        screen = ui.Screen.__new__(ui.Screen)
        screen.attr = {"claude": 1, "codex": 2, "dim": 3, "rule": 4, "menu_on": 5}
        screen.presence, screen.claims, screen.acks, screen.gate_snapshot = {}, {}, {}, None
        screen.task_list, screen.win, screen.rail_shown = [self.task(9, text=long), self.task(5)], self.Win(), True
        screen.todo_list = None
        jumped = []
        screen.go_to = jumped.append
        screen.draw_rail(30)
        row = next(y for y, text in screen.win.cells.items() if text.startswith("#9 "))
        self.assertTrue(screen.clicked(row + 1, 4))                  # its words, as well as its header
        self.assertEqual(jumped, [9])
        screen.win = self.Win()
        screen.draw_rail(30)
        rail = [screen.win.cells.get(y, "")[:render.RAIL_WIDTH].strip() for y in range(30)]
        start = rail.index(next(text for text in rail if text.startswith("#9 "))) + 1
        words = " ".join(rail[start:rail.index(next(text for text in rail if text.startswith("#5 ")))])
        self.assertEqual(" ".join(words.split()), long.removeprefix("@claude "))   # all of it, wrapped
        screen.highlight, screen.help, screen.view, screen.scroll, screen.draft, screen.images = None, False, None, 0, "", []
        screen.menu, screen.esc_at = lambda: [], 0
        screen.escape()
        self.assertIsNone(screen.rail_open)                          # Esc closes it again


class FormattingTests(unittest.TestCase):
    """Markdown-ish styles and coloured agent mentions in message text."""

    def spans(self, text):
        import curses
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight = None
        screen.attr = {"code": 1 << 20, "dim": 1 << 21, "me": 1 << 22, "claude_mention": 1 << 23, "codex_mention": 1 << 24}
        self.curses = curses
        return [(t, a) for t, a in screen._spans(text) if t != "\n"]

    def test_inline_styles(self):
        c = self.spans("a *it* _also_ ~~gone~~ snake_case_name 2*3*4 [docs](https://x.org/a) https://y.org")
        styled = {t: a for t, a in c}
        self.assertTrue(styled["it"] & render.ITALIC and styled["also"] & render.ITALIC)
        self.assertTrue(styled["gone"] & self.curses.A_DIM)
        self.assertTrue(styled["docs"] & self.curses.A_UNDERLINE)
        self.assertIn(" ↗ x.org", styled)                          # a web link names its site
        self.assertTrue(styled["https://y.org"] & self.curses.A_UNDERLINE)
        plain = "".join(t for t, _ in c)
        self.assertIn("snake_case_name 2*3*4", plain)            # words with _ or * inside stay as written

    def test_a_skills_line_is_grey_and_other_lines_are_not(self):
        spans = dict(self.spans("Done, merged.\nSkills: test-runs, release-readiness"))
        self.assertEqual(spans["Done, merged."], 0)
        self.assertEqual(spans["Skills: test-runs, release-readiness"], 1 << 21)
        self.assertEqual(dict(self.spans("Skills are fun"))["Skills are fun"], 0)   # only the "Skills:" line

    def test_wrapped_rows_keep_their_line_s_bar_gutter_or_hanging_indent(self):
        # UI hunt (2026-11-18): continuation rows of lists, quotes and code fell back to the bare margin
        self.spans("")                                # builds the screen
        from agi import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight = None
        screen.attr = {"code": 0, "dim": 0, "me": 0}
        hangs = []
        spans = screen._spans("- one two three four\n  - five six seven\n12. eight nine ten\n> ab cd ef gh ij\n"
                              "```\nalpha beta gamma\n```", hangs=hangs)
        rows = [row.rstrip() for row in flat(text.wrap([("  ", 0)], "  ", spans, 16, hangs=hangs))]
        self.assertEqual(rows, ["  • one two", "    three four", "    • five six", "      seven",
                                "  12. eight nine", "      ten", "  ▎ ab cd ef gh", "  ▎ ij", "  ──", "  │ alpha beta",
                                "  │ gamma", "  ──"])

    def test_a_hang_never_pushes_a_row_past_its_width(self):
        # a nested list in a narrow window gave every character its own over-wide row
        import random
        rng = random.Random(7)
        atoms = ["a", "bb", "dddd", "日本", "👍🏽", " ", "\n", "x" * 30]
        for _ in range(3000):
            width = rng.randint(4, 40)
            spans = [("".join(rng.choice(atoms) for _ in range(rng.randint(1, 12))), 0)]
            hangs = [[(" " * rng.randint(0, 30), 0)] if rng.random() < .7 else [("│ ", 0)] for _ in range(20)]
            for row in flat(text.wrap([("  ", 0)], "  ", spans, width, hangs=hangs)):
                self.assertLessEqual(text.text_width(row), max(width, 4), (width, spans))

    def test_line_styles(self):
        c = self.spans("> quoted words\n3. third\n- [ ] todo\n- [x] done\n---\n# Title")
        texts = [t for t, _ in c]
        self.assertEqual(texts[:2], ["▎ ", "quoted words"])
        self.assertTrue(c[1][1] & render.ITALIC)
        for marker in ("3. ", "☐ ", "☑ ", "─" * 12):
            self.assertIn(marker, texts)
        self.assertTrue(dict(c)["Title"] & self.curses.A_UNDERLINE)

    def test_long_unmatched_runs_stay_fast(self):
        for text_ in ("[" * 16000, "*a" * 8000, " _a" * 6000, "~~a" * 6000):
            started = time.monotonic()
            self.spans(text_)
            self.assertLess(time.monotonic() - started, 0.5, text_[:4])   # a backtracking pattern took 3.7 s

    def test_agent_mentions_have_their_own_colours(self):
        c = dict(self.spans("ping @claude and @codex, cc @user"))
        self.assertEqual((c["@claude"], c["@codex"], c["@user"]), (1 << 23, 1 << 24, 1 << 22))


class ReadMarkLayoutTests(ScreenCase):
    """Read marks on narrow screens: the id stays on the first row and nothing overflows."""

    def thread(self, cols, long_text):
        screen = self.open(0, rows=40, cols=cols)
        screen.chat("say", "--from", "user", "@claude @codex @admin " + long_text)    # 1
        screen.chat("say", "--from", "codex", "@claude and @codex done")               # 2
        woke(self.data, "claude", 2, first=1)
        woke(self.data, "codex", 1)
        screen.chat("ack", "--as", "claude", "2")
        admin_ack(self.data, 1)
        self.assertTrue(screen.until(lambda: "✓ #2" in screen.term.text()))
        self.assertTrue(screen.until(lambda: "✓ ○ ✓ #1" in screen.term.text()))
        return screen

    def test_marks_fit_forty_and_thirty_columns(self):
        for cols in (40, 30):
            with self.subTest(cols=cols):
                self.tearDown()
                self.setUp()
                screen = self.thread(cols, "a very long request with café and 日本語 words that wrap " * 3)
                lines = screen.term.text().splitlines()
                first = next(line for line in lines if line.startswith("❯ @claude"))
                self.assertTrue(first.endswith("✓ ○ ✓ #1 " + first[-5:]), first)      # the id stays on the first row
                self.assertLessEqual(text.text_width(first), cols)
                shared = next(line for line in lines if "✓ #2" in line)
                self.assertTrue(shared.startswith("● codex"), shared)
                self.assertTrue(all(text.text_width(line) <= cols for line in lines))


if __name__ == "__main__":
    unittest.main()
