"""Personal to-dos use the existing durable journal and timed checks."""

import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agi import bridge, store


SCRATCH = os.environ.get("AGI_SCRATCH", Path(__file__).resolve().parents[1])


class PersonalTodoTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="agi-personal-todo-", dir=SCRATCH)
        self.addCleanup(directory.cleanup)
        environment = mock.patch.dict(os.environ, AGI_DATA=directory.name)
        environment.start()
        self.addCleanup(environment.stop)
        importlib.reload(store)

    def test_personal_todo_replays_and_notifies_only_its_user(self):
        deadline = 1234567890
        item = store.add_personal_todo("  Check @all settings  ", due_at=deadline)
        origin, = store.read_messages()
        self.assertEqual((origin["id"], origin["sender"], origin["kind"]), (item["id"], "system", "system"))
        self.assertEqual((item["title"], item["next_action"]), ("Check @all settings", "Check @all settings"))
        self.assertEqual((item["source_id"], item["owner"], item["next_actor"]), (None, "user", "user"))
        self.assertFalse(bridge.relevant(origin))
        self.assertFalse(store.addressed_to(origin, "claude"))
        self.assertEqual(store.get_ack("codex"), 0)
        self.assertEqual(store.get_ack("claude"), 0)

        importlib.reload(store)
        self.assertEqual(store.list_todos(), [item])
        self.assertEqual(store.next_reminder_deadline(now=deadline - 1), deadline)
        notice, = store.dispatch_due_reminders(now=deadline)
        self.assertEqual(notice["text"].count("Check @all settings"), 1)
        self.assertTrue(store.addressed_to(notice, "user"))
        self.assertFalse(bridge.relevant(notice))
        self.assertFalse(store.addressed_to(notice, "claude"))

        importlib.reload(store)
        self.assertEqual(store.dispatch_due_reminders(now=deadline), [])
        self.assertEqual(store.list_todos(), [item])

    def test_retry_after_ambiguous_success_returns_the_same_item(self):
        write_record = store._write_record
        request_id = "1" * 32

        def lose_receipt(*args, **kwargs):
            write_record(*args, **kwargs)
            raise OSError("Lost receipt after writing")

        with mock.patch.object(store, "_write_record", side_effect=lose_receipt):
            with self.assertRaisesRegex(OSError, "after writing"):
                store.add_personal_todo("Check the result", request_id=request_id)

        importlib.reload(store)
        item = store.add_personal_todo("Check the result", request_id=request_id)
        self.assertEqual(store.list_todos(), [item])
        with self.assertRaisesRegex(ValueError, "different change"):
            store.add_personal_todo("Check another result", request_id=request_id)
        self.assertEqual(len(store.read_messages()), 1)

    def test_personal_and_linked_to_dos_share_the_same_projection(self):
        source = store.append_message("user", "Review the output")
        linked = store.add_todo("user", source["id"], "AGI", "Review", "user", "Open output")
        personal = store.add_personal_todo("Check the receipt")
        self.assertEqual(linked["source_id"], source["id"])
        self.assertIsNone(personal["source_id"])

        done = store.complete_todo("user", personal["id"], personal["revision"], "Receipt checked")
        importlib.reload(store)
        self.assertEqual(store.list_todos(), [linked])
        self.assertEqual(store.list_todos(include_done=True), [linked, done])


if __name__ == "__main__":
    unittest.main()
