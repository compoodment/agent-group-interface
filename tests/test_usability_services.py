"""Check read-only catch-up, one-shot probes and durable interface settings."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import connect, delivery_probe, history_view, membership, store, ui_state


class UsabilityServicesTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.data = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="agi-usability-", dir=os.environ.get("AGI_SCRATCH"),
        )))
        stack.enter_context(mock.patch.multiple(
            store, DATA_DIR=self.data, CHAT_DIR=self.data / "chat", CURSOR_DIR=self.data / "cursors",
        ))

    def test_find_filters_before_limiting_and_can_page(self):
        one = store.append_message("codex", "Compare middle colour")["id"]
        store.append_message("claude", "Compare middle colour")
        two = store.append_message("codex", "Compare middle colour again")["id"]
        first = history_view.find("compare middle from:codex colour", limit=1)
        self.assertEqual([row["id"] for row in first["messages"]], [two])
        self.assertTrue(first["more"])
        second = history_view.find("from:codex", before=two)
        self.assertEqual([row["id"] for row in second["messages"]], [one])
        self.assertFalse(second["more"])

    def test_catch_up_identifies_waiting_records_without_marking_unread_messages(self):
        source = store.append_message("user", "Check this example")["id"]
        ui_state.mark_read(source)
        store.append_message("claude", "An unrelated comment")
        reply = store.append_message("codex", f"↪ #{source} Here is the result")["id"]
        decision = store.append_message("claude", "Which option?",
                                        decision_prompt={"project": "AGI", "choices": ["1a", "1b"]})["id"]
        pending = store.add_todo("user", source, "AGI", "Review", "user", "Review it", next_actor="user")["id"]
        view = history_view.catch_up(source)
        self.assertEqual({(row["kind"], row["id"]) for row in view["waiting"]},
                         {("message", reply), ("decision", decision), ("todo", pending)})
        self.assertEqual(view["other_unread"], 1)
        self.assertEqual(ui_state.get_read_marker(), source)
        self.assertEqual(store.get_ack("claude"), 0)

    def test_probe_requires_selected_session_and_reserves_at_most_one_send(self):
        membership.observe("claude", "primary")
        with self.assertRaisesRegex(ValueError, "Join"):
            delivery_probe.request()
        selected = membership.request_join("claude", "primary")
        request = delivery_probe.request()
        self.assertEqual(request["state"], "queued")
        with self.assertRaisesRegex(ValueError, "pending"):
            delivery_probe.request()
        self.assertIsNone(delivery_probe.take_pending("another", selected["revision"]))
        self.assertEqual(delivery_probe.take_pending("primary", selected["revision"]), request["id"])
        self.assertIsNone(delivery_probe.take_pending("primary", selected["revision"]))
        with self.assertRaisesRegex(ValueError, "matching"):
            delivery_probe.confirm("another", request["id"])
        self.assertEqual(delivery_probe.confirm("primary", request["id"])["state"], "confirmed")
        self.assertEqual(store.get_ack("claude"), 0)
        pending = delivery_probe.request()
        membership.leave("claude")
        membership.resume("claude")
        self.assertEqual(delivery_probe.status()["state"], "cancelled")
        self.assertIsNone(delivery_probe.take_pending("primary", pending["revision"]))

    def test_probe_wakes_the_native_wait_without_appending_to_chat(self):
        membership.observe("claude", "primary")
        membership.request_join("claude", "primary")
        listening = threading.Event()
        select = store.select.select

        def watch(*args):
            listening.set()
            return select(*args)

        with (ThreadPoolExecutor(max_workers=1) as pool,
              mock.patch.object(store.select, "select", side_effect=watch)):
            future = pool.submit(store.wait_for_change, 0, 4,
                                 wake_when=lambda: delivery_probe.pending_for("primary"))
            self.assertTrue(listening.wait(2))
            delivery_probe.request()
            self.assertTrue(future.result(timeout=2))
        self.assertEqual(store.read_messages(), [])

    def test_confirmed_probe_does_not_carry_over_to_a_new_join(self):
        membership.observe("claude", "primary")
        membership.request_join("claude", "primary")
        probe = delivery_probe.request()
        delivery_probe.take_pending("primary", probe["revision"])
        self.assertEqual(delivery_probe.confirm("primary", probe["id"])["state"], "confirmed")
        membership.leave("claude")
        membership.resume("claude")
        self.assertEqual(delivery_probe.status()["state"], "cancelled")
        with self.assertRaisesRegex(ValueError, "matching"):
            delivery_probe.confirm("primary", probe["id"])

    def test_resume_command_targets_the_selected_provider_conversation(self):
        session = "11111111-1111-4111-8111-111111111111"
        self.assertIsNone(membership.resume_command("claude"))
        membership.observe("claude", session)
        membership.request_join("claude", session)
        membership.leave("claude")
        with mock.patch.object(connect, "find", return_value="/example dir/claude"):
            self.assertEqual(membership.resume_command("claude"),
                             f"'/example dir/claude' --resume {session} "
                             "--dangerously-load-development-channels server:agi")
        self.assertIsNotNone(membership.status("claude")["last_contact"])
        self.assertFalse(membership.status("claude")["joined"])

    def test_settings_persist_together_and_honor_explicit_environment_overrides(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ui_state.user_settings(), {"mouse": True, "notifications": True})
            ui_state.set_user_setting("mouse", False)
            self.assertEqual(ui_state.set_user_setting("notifications", False),
                             {"mouse": False, "notifications": False})
            with mock.patch.dict(os.environ, {"AGI_MOUSE": "1"}):
                self.assertEqual(ui_state.user_settings(), {"mouse": True, "notifications": False})
            self.assertEqual(json.loads((self.data / "ui/settings.json").read_text()),
                             {"version": 1, "mouse": False, "notifications": False})


if __name__ == "__main__":
    unittest.main()
