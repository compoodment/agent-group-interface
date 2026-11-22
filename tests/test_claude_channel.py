"""Claude channel tests: the MCP server runs as a subprocess on a throwaway data root."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRATCH = os.environ.get("AGI_SCRATCH")


ARRIVES = 10      # a notification that must come: returns when it does, so a loaded machine never fails it

class ChannelTest(unittest.TestCase):
    # tests that prove a message is NOT held: routine messages wait a minute there, so an arrival within ARRIVES
    # can only be the immediate path, however slow the machine (a 2.5 s race flaked under integration load)
    LONG_HOLD = {"test_user_message_notifies_quickly_and_ack_works", "test_code_and_quoted_mentions_do_not_wake",
                 "test_an_answer_to_claude_wakes_it_like_a_mention"}

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agi-channel-", dir=SCRATCH)
        quiet, hold = ("60", "120") if self._testMethodName in self.LONG_HOLD else ("1", "3")
        self.env = dict(os.environ, AGI_DATA=self.directory.name, CHANNEL_QUIET_S=quiet,
                        CHANNEL_HOLD_S=hold, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        for inherited in ("CLAUDE_CODE_SESSION_ID", "AGI_CHANNEL_SESSION"):   # run from inside a Claude session, the
            self.env.pop(inherited, None)                                    # tests mustn't share its ID
        self.proc = subprocess.Popen([sys.executable, "-m", "agi.claude_channel"], cwd=ROOT, env=self.env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True)
        self.lines, self.raw, self.next_id = queue.Queue(), [], 1
        threading.Thread(target=self.pump, daemon=True).start()
        # a store in this process on the same data root, for posting as other senders
        os.environ["AGI_DATA"] = self.directory.name
        from agi import store
        import importlib
        self.store = importlib.reload(store)

    def tearDown(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()
        os.environ.pop("AGI_DATA", None)
        self.directory.cleanup()

    def pump(self):
        for line in self.proc.stdout:
            self.raw.append(line)
            self.lines.put(line)

    def get(self, timeout=5):
        try:
            return json.loads(self.lines.get(timeout=timeout))
        except queue.Empty:
            return None

    def request(self, method, params=None):
        ident, self.next_id = self.next_id, self.next_id + 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            message = self.get(ARRIVES)             # a reply must come: on a loaded machine it can take over 5 s
            self.assertIsNotNone(message, f"no reply to {method}")
            if message.get("id") == ident:
                return message

    def start(self, version="2025-06-18", join=True):
        """The client's handshake, then, as the person says "join the chat", chat_join (#6019: joining is explicit)."""
        reply = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "0"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        if join:
            self.assertIn("joined", self.call("chat_join"))
        return reply

    def call(self, name, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["content"][0]["text"]

    def notification(self, timeout):
        while True:
            message = self.get(timeout)
            if message is None or message.get("method") == "notifications/claude/channel":
                return message

    def test_initialize_and_tools(self):
        self.proc.stdin.buffer.write(b"\xff\n")
        self.proc.stdin.flush()
        self.assertEqual(self.get(ARRIVES)["error"]["code"], -32700)
        result = self.start("future-fixture")["result"]
        self.assertIn("claude/channel", result["capabilities"]["experimental"])
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        names = {t["name"] for t in self.request("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"chat_reply", "chat_ack", "chat_release_notes", "chat_take", "chat_doing", "chat_done",
                                 "chat_read", "chat_join", "chat_leave", "chat_confirm_delivery", "chat_evidence"})
        self.assertEqual(self.request("ping")["result"], {})

    def test_nothing_arrives_until_the_session_joins(self):
        # #6019: a window started without the flag loads the server too, so a session joins only when asked
        self.start(join=False)
        self.store.append_message("user", "@claude are you there?")
        notice = json.dumps(self.notification(ARRIVES))                 # only the one notice: no message text
        self.assertIn("no Claude session has joined", notice)
        self.assertNotIn("are you there?", notice)
        self.assertIn("joined", self.call("chat_join"))
        delivered = json.dumps(self.notification(ARRIVES))
        self.assertIn("are you there?", delivered)
        self.assertIn("1 message arrived while this session was away", delivered)

    def test_a_resumed_claude_session_is_still_the_joined_one_after_a_restart(self):
        # #6032: after a reboot, claude --continue resumes the same conversation with the same CLAUDE_CODE_SESSION_ID;
        # the chat still delivers to it without anyone approving it again, and a session left before stays quiet
        from agi import membership
        self.proc.stdin.close()
        self.proc.wait(5)
        self.proc.stdout.close()
        conversation = "5f0c2a1e-0000-4000-8000-00000000c1a0"

        def boot():
            self.env["CLAUDE_CODE_SESSION_ID"] = conversation
            self.proc = subprocess.Popen([sys.executable, "-m", "agi.claude_channel"], cwd=ROOT, env=self.env,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         text=True)
            self.lines, self.raw, self.next_id = queue.Queue(), [], 1
            threading.Thread(target=self.pump, daemon=True).start()

        boot()
        self.start()
        self.assertEqual(membership.status("claude")["session"], conversation)
        self.proc.stdin.close()                                         # the laptop goes down
        self.proc.wait(5)
        boot()
        self.start(join=False)                                          # nobody says "join" after the reboot
        self.store.append_message("user", "@claude back after the reboot?")
        self.assertIn("back after the reboot", json.dumps(self.notification(ARRIVES)))

    def test_claude_hears_at_once_when_the_person_takes_a_request_back(self):
        # #6036: /unclaim writes a system record; Claude must not keep working on a request no longer its own
        self.start()
        request = self.store.append_message("user", "please look at the footer")
        self.call("chat_take", id=request["id"])
        while self.notification(2):                                     # the request itself, delivered first
            pass
        claim = self.store.task_claim(request["id"])
        self.store.release_task_claim(request["id"], claim["claim_id"])
        delivered = json.dumps(self.notification(ARRIVES))
        self.assertIn(f"The person took #{request['id']} back from you", delivered)

    def test_a_session_nobody_joined_is_told_once_that_messages_wait(self):
        # 2026-11-22: after the update, the channel went quiet and so did Claude, since it's what wakes Claude
        self.start(join=False)
        self.store.append_message("user", "@claude are you there?")
        self.store.append_message("user", "@claude hello?")
        notice = json.dumps(self.notification(ARRIVES))
        self.assertIn("are waiting, but no Claude session has joined the chat", notice)
        self.assertNotIn("are you there?", notice)                      # the messages only after joining
        self.store.append_message("user", "@claude still?")
        self.assertIsNone(self.notification(3))                         # once, not per message
        self.call("chat_join")
        self.assertIn("still?", json.dumps(self.notification(ARRIVES)))

    def test_a_session_that_left_by_choice_is_not_nudged(self):
        self.start()
        self.call("chat_leave")
        self.store.append_message("user", "@claude anything?")
        self.assertIsNone(self.notification(3))

    def test_a_delivery_test_reaches_the_joined_session_and_comes_back_confirmed(self):
        from agi import delivery_probe
        self.start()
        before = self.store.get_ack("claude")
        delivery_probe.request()
        notice = self.notification(ARRIVES)
        probe = notice["params"]["meta"]["delivery_test"]
        self.assertIn("AGI delivery test", notice["params"]["content"])
        self.assertIn("confirmed", self.call("chat_confirm_delivery", id=probe))
        self.assertEqual(delivery_probe.status()["state"], "confirmed")
        self.assertEqual(self.store.get_ack("claude"), before)
        self.assertEqual([m for m in self.store.read_messages(0) if m["kind"] == "msg"], [])   # nothing posted

    def test_evidence_suggests_the_skills_recorded_since_the_claim(self):
        # #6059: skills come from receipts, not memory; the agent reviews them before chat_done
        from agi import skills
        self.start()
        request = self.store.append_message("user", "@claude please tidy the footer")
        self.call("chat_take", id=request["id"])
        found = self.call("chat_evidence", id=request["id"])
        self.assertIn("skills: none recorded since the claim", found)
        self.assertIn("Review both", found)
        shelf = Path(self.directory.name) / "skills-shelf"               # an installed skill, read after the claim
        (shelf / "clean-ui").mkdir(parents=True)
        (shelf / "clean-ui" / "SKILL.md").write_text("# Clean UI\n")
        os.environ["AGI_SKILLS_DIR"] = str(shelf)
        try:
            skills.read("claude", "clean-ui")
        finally:
            os.environ.pop("AGI_SKILLS_DIR")
        self.assertIn("skills: clean-ui", self.call("chat_evidence", id=request["id"]))

    def reboot_into(self, conversation):
        """The laptop goes down and Claude's conversation is resumed: a new channel process with the same ID."""
        self.proc.stdin.close()
        self.proc.wait(5)
        self.proc.stdout.close()
        self.env["CLAUDE_CODE_SESSION_ID"] = conversation
        self.proc = subprocess.Popen([sys.executable, "-m", "agi.claude_channel"], cwd=ROOT, env=self.env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.lines, self.raw, self.next_id = queue.Queue(), [], 1
        threading.Thread(target=self.pump, daemon=True).start()

    def test_reboot_with_two_sessions_in_one_folder_keeps_the_joined_one(self):
        # the reboot rehearsal (#6032, #6130): two Claude conversations in one folder; after a reboot only the one
        # that was joined gets messages, without anyone saying join, and the other doesn't take over
        from agi import membership
        joined, other = "5f0c2a1e-0000-4000-8000-0000000000a1", "5f0c2a1e-0000-4000-8000-0000000000b2"
        self.reboot_into(joined)
        self.start()
        membership.observe("claude", other)
        self.assertEqual(membership.request_join("claude", other)["pending"], other)   # it can only ask
        self.reboot_into(joined)
        self.start(join=False)
        self.store.append_message("user", "@claude after the reboot")
        self.assertIn("after the reboot", json.dumps(self.notification(ARRIVES)))
        self.assertEqual(membership.status("claude")["session"], joined)

    def test_reboot_after_leaving_stays_quiet(self):
        # the reboot rehearsal (#6032, #6130): a conversation that left before shutdown reopens quietly: no
        # messages and no "messages are waiting" nudge, until the person asks it to join
        conversation = "5f0c2a1e-0000-4000-8000-0000000000c3"
        self.reboot_into(conversation)
        self.start()
        self.call("chat_leave")
        self.reboot_into(conversation)
        self.start(join=False)
        self.store.append_message("user", "@claude are you back?")
        self.assertIsNone(self.notification(3))

    def test_leaving_stops_delivery_and_rejoining_brings_the_missed_ones(self):
        self.start()
        self.assertIn("left", self.call("chat_leave"))
        self.store.append_message("user", "@claude one")
        self.store.append_message("user", "@claude two")
        self.assertIsNone(self.notification(3))
        from agi import membership
        status = membership.status("claude")
        self.assertFalse(status["joined"])
        self.assertIsNotNone(status["session"])                         # left by choice, not never-joined
        membership.resume("claude")                                     # /join claude, from the chat screen
        delivered = json.dumps(self.notification(ARRIVES))
        self.assertIn("2 messages arrived while this session was away", delivered)
        self.assertIn("one", delivered)
        self.assertIn("two", delivered)

    def test_leaving_and_rejoining_between_two_looks_still_catches_up(self):
        # Codex (#6047): joined before and after, but a new revision in between is a rejoin, from the ack
        import io
        from unittest import mock
        from agi import claude_channel, membership
        out = io.StringIO()
        channel = claude_channel.Channel(out=out)
        membership.observe("claude", channel.session)
        membership.request_join("claude", channel.session)
        self.assertTrue(channel.joined())
        first = self.store.append_message("user", "@claude missed while away")
        channel.delivered = first["id"]                                 # read, but not handled (no ack)
        membership.leave("claude")
        membership.resume("claude")                                     # both before the channel looks again
        self.assertTrue(channel.joined())
        self.assertTrue(channel.away)                                   # treated as a rejoin
        self.assertFalse(membership.can_deliver("claude", channel.session, revision="0" * 32))

    def test_a_second_session_asks_and_the_person_moves_claude_there(self):
        self.start()
        other = subprocess.Popen([sys.executable, "-m", "agi.claude_channel"], cwd=ROOT, env=self.env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

        def stop_other():                           # before tearDown removes the data folder (an addCleanup runs
            other.stdin.close()                     # after it, and the gate caught this one still writing, #6040)
            try:
                other.wait(5)
            except subprocess.TimeoutExpired:
                other.kill()
                other.wait(5)
            other.stdout.close()
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in other.stdout], daemon=True).start()

        def ask(method, params, ident):
            other.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
            other.stdin.flush()
            while True:
                message = json.loads(lines.get(timeout=ARRIVES))
                if message.get("id") == ident:
                    return message

        try:
            ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}, 1)
            other.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            other.stdin.flush()
            answer = ask("tools/call", {"name": "chat_join", "arguments": {}}, 2)["result"]["content"][0]["text"]
            self.assertIn("asked to join", answer)
            from agi import membership
            pending = membership.status("claude")["pending"]
            self.assertIsNotNone(pending)
            membership.resume("claude", session=pending)                    # the person says yes in the chat screen
            self.store.append_message("user", "@claude which one of you gets this?")
            self.assertIsNone(self.notification(3))                         # the first session no longer
            while True:
                message = json.loads(lines.get(timeout=ARRIVES))
                if message.get("method") == "notifications/claude/channel":
                    break
            self.assertIn("which one of you", json.dumps(message))
        finally:
            stop_other()

    def test_unknown_method_is_error(self):
        self.start()
        self.assertEqual(self.request("nope/nothing")["error"]["code"], -32601)

    def relaunch_from_a_copy(self, run_from=None):
        """The channel run from a copy of the code that the test can change under it, checking it every 0.2 s;
        run_from(copy) can return another path to start it through (an install's `current` link)."""
        import shutil
        self.proc.stdin.close()
        self.proc.wait(5)
        self.proc.stdout.close()
        copy = Path(self.directory.name) / "code"
        shutil.copytree(ROOT / "agi", copy / "agi", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("VERSION", "CHANGELOG.md"):                     # read by the package at import
            shutil.copy(ROOT / name, copy / name)
        start = run_from(copy) if run_from else copy
        env = dict(self.env, PYTHONPATH=str(start), CHANNEL_CODE_CHECK_S="0.2", CHANNEL_SETTLE_CODE_S="0.4")
        self.proc = subprocess.Popen([sys.executable, "-P", "-m", "agi.claude_channel"], cwd=start, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True)
        self.lines, self.raw, self.next_id = queue.Queue(), [], 1
        threading.Thread(target=self.pump, daemon=True).start()
        return copy / "agi" / "claude_channel.py"

    def tool_names(self):
        return {tool["name"] for tool in self.request("tools/list")["result"]["tools"]}

    def test_new_code_on_disk_takes_over_the_same_connection(self):
        # The new code keeps the transport and announces the changed tool list.
        source = self.relaunch_from_a_copy()
        self.start()
        pid = self.proc.pid
        self.store.append_message("user", "@claude before the change")
        self.assertIsNotNone(self.notification(ARRIVES))
        self.assertNotIn("chat_probe", self.tool_names())
        code = source.read_text()
        source.write_text(code.replace('TOOLS = [\n', 'TOOLS = [\n    tool("chat_probe", "A tool only the new code has.", {}),\n', 1))
        deadline = time.time() + 20
        while time.time() < deadline and "chat_probe" not in self.tool_names():
            time.sleep(0.3)
        self.assertIn("chat_probe", self.tool_names())                  # the new code answers
        self.assertEqual(self.proc.pid, pid)                           # in the same process, on the same pipes
        self.assertTrue(any("tools/list_changed" in line for line in self.raw))
        result = self.request("tools/call", {"name": "chat_reply", "arguments": {"text": "after the change"}})["result"]
        self.assertFalse(result["isError"])                            # a reply after it lands exactly once
        self.assertEqual([m["text"] for m in self.store.read_messages()].count("after the change"), 1)
        self.store.append_message("user", "@claude after the restart")
        delivered = self.notification(ARRIVES)
        self.assertIn("after the restart", json.dumps(delivered))
        self.assertNotIn("before the change", json.dumps(delivered))    # nothing handed over twice

    def test_an_update_that_moves_the_current_link_takes_over(self):
        # an install keeps versions side by side and switches `current` in one step (#5968, Codex #5963). A channel
        # registered through the link (agi connect does that) sees the switch, because code_stamp() resolves the link
        # on every check, and restarts into the new version, because PYTHONPATH still names the link
        import shutil
        prefix = Path(self.directory.name) / "install"

        def install(copy):
            (prefix / "versions").mkdir(parents=True)
            shutil.copytree(copy, prefix / "versions" / "a")
            shutil.copytree(copy, prefix / "versions" / "b")
            new = prefix / "versions" / "b" / "agi" / "claude_channel.py"
            new.write_text(new.read_text().replace(
                'TOOLS = [\n', 'TOOLS = [\n    tool("chat_probe", "A tool only the new version has.", {}),\n', 1))
            (prefix / "current").symlink_to("versions/a")
            return prefix / "current"

        self.relaunch_from_a_copy(install)
        self.start()
        pid = self.proc.pid
        self.assertNotIn("chat_probe", self.tool_names())
        (prefix / "current.new").symlink_to("versions/b")
        os.replace(prefix / "current.new", prefix / "current")           # the update's one-step switch
        deadline = time.time() + 20
        while time.time() < deadline and "chat_probe" not in self.tool_names():
            time.sleep(0.3)
        self.assertIn("chat_probe", self.tool_names())
        self.assertEqual(self.proc.pid, pid)

    def test_broken_new_code_keeps_the_running_version(self):
        source = self.relaunch_from_a_copy()
        self.start()
        source.write_text(source.read_text() + "\nthis is not python\n")
        time.sleep(2)                                                  # well past settling and the import check
        result = self.request("tools/call", {"name": "chat_reply", "arguments": {"text": "still here"}})["result"]
        self.assertFalse(result["isError"])
        self.assertIsNone(self.proc.poll())

    def test_a_client_that_leaves_wins_over_a_reload(self):
        source = self.relaunch_from_a_copy()
        self.start()
        source.write_text(source.read_text() + "\n# changed\n")
        self.proc.stdin.close()                                        # the client goes as the change lands
        self.assertEqual(self.proc.wait(10), 0)

    def test_a_reply_with_choices_is_a_marked_question_and_its_answer_a_decision(self):
        # T4178: Claude marks a question in the reply itself, not with a separate command it could forget
        self.start()
        result = self.request("tools/call", {"name": "chat_reply", "arguments": {
            "text": "Which one? 1a red 1b blue", "choices": ["1a", "1b"], "project": "AGI"}})["result"]
        self.assertFalse(result["isError"], result)
        question = self.store.read_messages()[-1]
        self.assertEqual(question["decision_prompt"], {"project": "AGI", "choices": ["1a", "1b"]})
        self.store.append_message("user", f"↪ #{question['id']} 1b")
        [decision] = self.store.list_decisions()
        self.assertEqual(decision["selections"]["1"]["choice"], "1b")
        missing = self.request("tools/call", {"name": "chat_reply", "arguments": {
            "text": "Which? 1a 1b", "choices": ["1a", "1b"]}})["result"]
        self.assertTrue(missing["isError"])                     # a question needs its project
        self.assertIn("project", missing["content"][0]["text"])

    def test_reply_appends_as_claude(self):
        self.start()
        result = self.request("tools/call", {"name": "chat_reply", "arguments": {"text": "hi there"}})["result"]
        self.assertFalse(result["isError"])
        last = self.store.read_messages()[-1]
        self.assertEqual((last["sender"], last["text"]), ("claude", "hi there"))

    def test_a_reply_whose_write_landed_before_an_error_retries_without_a_second_copy(self):
        # Codex's audit (#5554): the reply was appended, a directory sync then failed and returned an error, and the
        # retry posted a second copy. The error now gives the post's request id, and a retry with it returns the
        # first post
        import io
        from unittest import mock
        from agi import claude_channel
        channel = claude_channel.Channel(out=io.StringIO())
        real, failed = self.store._sync_directory, []

        def once(path):                     # the first sync after the append fails; the record is already written
            if not failed:
                failed.append(path)
                raise OSError(5, "Input/output error")
            return real(path)

        with mock.patch.object(self.store, "_sync_directory", once):
            text, error = channel.call_tool("chat_reply", {"text": "posted once"})
        self.assertTrue(error, text)
        self.assertEqual([m["text"] for m in self.store.read_messages()].count("posted once"), 1)   # it did land
        self.assertIn("request_id ", text)
        request_id = text.rsplit("request_id ", 1)[1].split()[0]
        text, error = channel.call_tool("chat_reply", {"text": "posted once", "request_id": request_id})
        self.assertFalse(error, text)
        copies = [m for m in self.store.read_messages() if m["text"] == "posted once"]
        self.assertEqual(len(copies), 1)
        self.assertIn(f"posted #{copies[0]['id']}", text)

    def test_a_reply_into_codexs_request_says_whose_it_is_and_what_codex_is_doing(self):
        # whose request it went into and what they're doing, and Claude's own claims carry what Claude is doing
        self.start()
        asked = self.store.append_message("user", "rename the repo")
        self.store.take_task("codex", asked["id"], doing="renaming the GitHub repositories")
        call = lambda name, **arguments: self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        result = call("chat_reply", text=f"↪ #{asked['id']} I can do that")
        self.assertIn(f"note: #{asked['id']} is codex's, doing: renaming the GitHub repositories",
                      result["content"][0]["text"])
        mine = self.store.append_message("user", "and the docs")
        self.assertFalse(call("chat_take", id=mine["id"], doing="writing the docs")["isError"])
        self.assertFalse(call("chat_doing", id=mine["id"], doing="checking the links")["isError"])
        own = call("chat_reply", text=f"↪ #{mine['id']} on it")
        self.assertNotIn("note:", own["content"][0]["text"])           # Claude's own request: nothing to warn about

    def test_a_promise_for_later_comes_back_as_a_question_not_a_todo(self):
        self.start()
        asked = self.store.append_message("user", "fix the footer")
        call = lambda text: self.request("tools/call", {"name": "chat_reply",
                                                        "arguments": {"text": text}})["result"]["content"][0]["text"]
        before = len(self.store.list_todos(include_done=True))
        promised = call(f"↪ #{asked['id']} Done for the page.\nI'll check the phone layout later.")
        self.assertIn("question:", promised)
        self.assertIn("I'll check the phone layout later.", promised)
        self.assertEqual(len(self.store.list_todos(include_done=True)), before)     # asked, never recorded by itself
        self.assertNotIn("question:", call(f"↪ #{asked['id']} The footer is fixed and live."))

    def test_user_message_notifies_quickly_and_ack_works(self):
        self.start()
        user = self.store.append_message("user", "hello claude")
        note = self.notification(ARRIVES)
        self.assertIsNotNone(note)
        self.assertEqual(note["params"]["meta"]["last_id"], str(user["id"]))
        self.assertIn(f"#{user['id']} user:\nhello claude", note["params"]["content"])
        self.assertEqual(self.store.get_ack("claude"), 0)  # never acked by the server itself
        ack = self.request("tools/call", {"name": "chat_ack", "arguments": {"id": user["id"]}})["result"]
        self.assertFalse(ack["isError"])
        self.assertEqual(self.store.get_ack("claude"), user["id"])
        bad = self.request("tools/call", {"name": "chat_ack", "arguments": {"id": 999}})["result"]
        self.assertTrue(bad["isError"])

    def test_restart_replays_unacked(self):
        user = self.store.append_message("user", "before start")
        self.start()
        note = self.notification(ARRIVES)
        self.assertIn(f"#{user['id']}", note["params"]["content"])

    def test_routine_messages_batched_and_claims_skipped(self):
        self.start()
        self.store.append_message("codex", "one")
        self.store.append_message("codex", "src/a.py", kind="claim")
        self.store.append_message("codex", "two")
        self.assertIsNone(self.notification(0.5))  # held, not immediate
        note = self.notification(ARRIVES)
        self.assertIsNotNone(note)
        content = note["params"]["content"]
        self.assertEqual(len(content.split("\n\n")), 2)     # two messages, each a header and its text
        self.assertIn("one", content)
        self.assertIn("two", content)
        self.assertNotIn("src/a.py", content)
        self.assertEqual(note["params"]["meta"]["senders"], "codex")

    def test_events_carry_exact_text(self):
        # the real run (#1176-): a 582-character message lost its tail, and a typed ⏎ looked like a line break
        self.start()
        text = "burst A: " + "0123456789" * 55 + " — exact tail 🟣\nreal newline, literal ⏎ kept"
        self.store.append_message("user", text)
        note = self.notification(ARRIVES)
        self.assertIn(text, note["params"]["content"])
        read = self.request("tools/call", {"name": "chat_read", "arguments": {"after": 0}})["result"]
        self.assertIn(text, read["content"][0]["text"])

    def test_no_message_is_cut_or_skipped_at_the_event_size_limit(self):
        # Codex's review: a first message over the limit wasn't capped, and an omitted later one still showed as
        # last_id, so acking it would skip text Claude never saw
        self.start()
        big = "x" * 16050
        sent = [self.store.append_message("user", big)["id"], self.store.append_message("user", "small")["id"],
                self.store.append_message("user", "y" * 16050)["id"]]
        seen = []
        for _ in range(3):
            note = self.notification(ARRIVES)
            self.assertIsNotNone(note, f"after {seen}")
            ids = [int(n) for n in __import__("re").findall(r"^#(\d+) user:$", note["params"]["content"], 8)]
            self.assertEqual(int(note["params"]["meta"]["last_id"]), ids[-1])   # last_id = last carried
            seen += ids
            if seen == sent:
                break
        self.assertEqual(seen, sent)
        self.assertIn(big, "".join(self.raw_contents()))

    def raw_contents(self):
        return [json.loads(line)["params"]["content"] for line in self.raw if '"notifications/claude/channel"' in line]

    def test_read_after_zero_starts_at_the_beginning(self):
        for n in range(21):
            self.store.append_message("user", f"record {n}")
        self.start()
        text = self.request("tools/call", {"name": "chat_read", "arguments": {"after": 0}})["result"]["content"][0]["text"]
        self.assertTrue(text.startswith("#1 user:"), text[:30])

    def test_delivery_and_acks_leave_the_work_state_alone(self):
        # was working (they marked "working" until an ack, then "listening" through hours of work); now the
        # channel only keeps Claude reachable, and work comes from Claude's own turns
        self.start()
        deadline = time.monotonic() + ARRIVES                    # the channel's loop renews it once running
        while not self.store.get_presence()["claude"]["reachable"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.store.get_presence()["claude"]["reachable"])
        self.store.set_presence("claude", "working", ttl=300)          # as the turn hook reports it
        user = self.store.append_message("user", "a question")
        self.assertIsNotNone(self.notification(ARRIVES))
        self.request("tools/call", {"name": "chat_ack", "arguments": {"id": user["id"]}})
        presence = self.store.get_presence()["claude"]
        self.assertEqual((presence["state"], presence["reachable"]), ("working", True))
        # /who can time the wake and the handling. The channel writes the receipt just after the notification
        # goes out, so a busy drive can land it after the ack (the gate run of b6c9468, "drive busy"): wait for it
        deadline = time.monotonic() + ARRIVES
        while (last := self.store.wake_timing("claude")["last"]) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(last, "no wake receipt")
        self.assertEqual(last["id"], user["id"])
        self.assertIsNotNone(last["wake_s"])
        self.assertIsNotNone(last["ack_s"])

    def test_a_failing_status_write_does_not_stop_delivery(self):
        # Codex (#1242): a status write error ended the watcher, after which messages stopped silently
        import io
        from unittest import mock
        from agi import claude_channel
        out = io.StringIO()
        channel = claude_channel.Channel(out=out)
        claude_channel.membership.observe("claude", channel.session)       # the session joined, as a person asks
        claude_channel.membership.request_join("claude", channel.session)
        wait = claude_channel.store.wait_for_change
        with mock.patch.object(claude_channel.store, "set_reachable", side_effect=OSError("drive busy")), \
                mock.patch.object(claude_channel.store, "wait_for_change",   # short, so it can stop promptly
                                  lambda after, timeout, **kw: wait(after, timeout=min(timeout, 0.2), **kw)):
            watcher = threading.Thread(target=channel.watch, daemon=True)
            watcher.start()
            try:
                for text in ("first", "second"):
                    self.store.append_message("user", text)
                    deadline = time.monotonic() + 10
                    while text not in out.getvalue() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertIn(text, out.getvalue())             # delivered, and still watching after
            finally:
                channel.stopping.set()                              # nothing may keep writing into this folder
                watcher.join(5)
        self.assertFalse(watcher.is_alive())

    def test_an_ack_writes_only_the_inbox_cursor(self):
        # Codex (#1244): an ack once failed on its status write after the cursor committed; it writes no status
        # at all now (#1512), so presence failing can't touch it
        import io
        from unittest import mock
        from agi import claude_channel
        channel = claude_channel.Channel(out=io.StringIO())
        request = self.store.append_message("user", "a question")
        with mock.patch.object(claude_channel.store, "set_presence", side_effect=OSError("drive busy")), \
                mock.patch.object(claude_channel.store, "set_reachable", side_effect=OSError("drive busy")):
            text, failed = channel.call_tool("chat_ack", {"id": request["id"]})
        self.assertFalse(failed, text)
        self.assertEqual(self.store.get_ack("claude"), request["id"])

    def test_due_reminder_only_notifies_the_current_owner(self):
        items = {}
        for owner in ("codex", "claude"):
            task = self.store.append_message("user", "check the build")
            self.store.take_task(owner, task["id"])
            self.store.schedule_reminder(owner, task["id"], time.time())
            items[owner] = self.store.add_todo("user", task["id"], "AGI", "Check build", owner,
                                               "Confirm it launches.", due_at=time.time())
        self.store.set_ack("claude", self.store.last_id())
        self.start()
        notification = self.notification(ARRIVES)
        self.assertIsNotNone(notification)
        self.assertIn("@claude Reminder", notification["params"]["content"])
        self.assertNotIn("@codex Reminder", notification["params"]["content"])
        self.assertIn(f"@claude T{items['claude']['id']} is due.", notification["params"]["content"])
        self.assertNotIn(f"@codex T{items['codex']['id']} is due.", notification["params"]["content"])
        self.assertIsNone(self.notification(1.2))

    def test_an_answer_cue_wakes_only_the_to_dos_owner(self):
        # The answer cue reaches only the to-do's owner.
        items = {}
        for owner in ("codex", "claude"):
            question = self.store.append_message(owner, f"{owner}'s question: 1a or 1b?")
            items[owner] = self.store.add_todo(owner, question["id"], "AGI", f"{owner}'s choice", owner,
                                               "Wait for the user's pick.", next_actor="user")
            self.store.append_message("user", f"↪ #{question['id']} 1a")
        self.store.set_ack("claude", self.store.last_id())
        self.start()
        self.store.dispatch_todo_answers()
        notification = self.notification(ARRIVES)
        self.assertIsNotNone(notification)
        self.assertIn(f"T{items['claude']['id']}", notification["params"]["content"])
        self.assertNotIn(f"T{items['codex']['id']}", notification["params"]["content"])
        self.assertIsNone(self.notification(1.2))

    def test_code_and_quoted_mentions_do_not_wake(self):
        self.start()
        self.store.append_message("codex", "a code example: `@all` and `@claude`")
        self.assertIsNone(self.notification(0.8))           # held like any routine message
        self.store.append_message("codex", "@claude this one is real")
        note = self.notification(ARRIVES)                   # the held example comes with it, not before it
        self.assertIsNotNone(note)
        self.assertEqual(note["params"]["meta"]["count"], "2")

    def test_an_answer_to_claude_wakes_it_like_a_mention(self):
        # live run (#1665/#1666): Codex's '↪ #1665' answer to Claude's #1665 waited out the 45 s quiet
        self.start()
        mine = self.store.append_message("claude", "a question for codex")
        self.store.append_message("codex", f"↪ #{mine['id'] - 1} about something else")   # not Claude's
        self.assertIsNone(self.notification(0.8))           # held like any routine message
        self.store.append_message("codex", f"↪ #{mine['id']} here is the answer")
        note = self.notification(ARRIVES)
        self.assertIsNotNone(note)
        self.assertIn("here is the answer", note["params"]["content"])

    def test_a_burst_of_urgent_messages_wakes_once(self):
        self.start()
        for n in range(3):                                   # 40 ms apart, as in the real run
            self.store.append_message("user", f"burst {n}")
            time.sleep(0.04)
        note = self.notification(ARRIVES)
        self.assertEqual(note["params"]["meta"]["count"], "3")
        self.assertIsNone(self.notification(1))

    def test_own_messages_not_echoed(self):
        self.start()
        self.request("tools/call", {"name": "chat_reply", "arguments": {"text": "mine"}})
        self.assertIsNone(self.notification(2.5))

    def test_take_and_read(self):
        self.start()
        user = self.store.append_message("user", "please do x")
        self.notification(ARRIVES)
        taken = self.request("tools/call", {"name": "chat_take", "arguments": {"id": user["id"]}})["result"]
        self.assertFalse(taken["isError"])
        again = self.request("tools/call", {"name": "chat_take", "arguments": {"id": 99}})["result"]
        self.assertTrue(again["isError"])
        call = lambda name, **args: self.request("tools/call", {"name": name, "arguments": args})["result"]
        ack = call("chat_ack", id=user["id"])["content"][0]["text"]
        self.assertIn(f"still holding #{user['id']}", ack)                # a claim left behind is pointed out
        bare = call("chat_done", id=user["id"], result="did x")
        self.assertTrue(bare["isError"])                                  # proof first (#5649 2a)
        self.assertIn("check", bare["content"][0]["text"])
        self.assertIn(f"still holding #{user['id']}", call("chat_ack", id=user["id"])["content"][0]["text"])
        done = call("chat_done", id=user["id"], result="did x", check="test_x passes", skills="test-runs")
        self.assertFalse(done["isError"])
        self.assertEqual(self.store.list_tasks()[0]["check"], "test_x passes")
        self.assertEqual(self.store.list_tasks()[0]["state"], "done")
        self.assertNotIn("holding", call("chat_ack", id=user["id"])["content"][0]["text"])
        self.assertTrue(call("chat_done", id=99, result="x", check="y", skills="z")["isError"])   # only Claude's
        text = self.request("tools/call", {"name": "chat_read", "arguments": {}})["result"]["content"][0]["text"]
        self.assertIn("please do x", text)

    def test_stdout_is_only_json(self):
        self.start()
        self.store.append_message("user", "x")
        self.notification(ARRIVES)
        self.proc.stdin.write("garbage\n")
        self.proc.stdin.flush()
        self.assertEqual(self.get()["error"]["code"], -32700)
        for raw in self.raw:
            self.assertEqual(json.loads(raw)["jsonrpc"], "2.0")


if __name__ == "__main__":
    unittest.main()
