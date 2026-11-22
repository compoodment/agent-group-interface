"""Recover real saved chat, cursor and draft state from a backup."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import backup, cli, membership, store, ui_state


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="agi-backup-", dir=os.environ.get("AGI_SCRATCH"),
        )))
        self.data = self.root / "chat-data"
        self.stack.enter_context(self.use_conversation(self.data))
        self.first = store.append_message("user", "A first message")["id"]
        self.next = store.append_message("user", "Still unread")["id"]
        store.set_ack("codex", self.first)
        ui_state.mark_read(self.first)

    def use_conversation(self, directory):
        return mock.patch.multiple(store, DATA_DIR=directory, CHAT_DIR=directory / "chat",
                                   CURSOR_DIR=directory / "cursors")

    def test_cli_restores_recent_draft_and_both_read_positions(self):
        older = ui_state.DraftSession()
        self.addCleanup(older.close)
        older.save("Earlier draft", 13, {}, 0)
        latest = ui_state.DraftSession()
        self.addCleanup(latest.close)
        latest.save("Latest draft", 12, {}, 0)
        item = store.add_personal_todo("Review the recovered example")
        membership.observe("claude", "selected-session")
        membership.request_join("claude", "selected-session")
        ui_state.set_user_setting("notifications", False)
        receipt = self.data / "admin-mentions/cursor.json"
        receipt.parent.mkdir()
        receipt.write_text('{"version":1,"read_through":1}')
        (self.data / "ui/sounds.log").write_text("playing sound")
        original = {p.relative_to(self.data): p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["backup"]), 0)
        archive = Path(output.getvalue().strip())
        self.assertEqual(archive.parent, self.root / "chat-data-backups")
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with zipfile.ZipFile(archive) as contents:
            self.assertIsNone(contents.testzip())
            record = json.loads(contents.read("backup.json"))
            self.assertNotIn("ui/sounds.log", record["files"])
            self.assertIn("admin-mentions/cursor.json", record["files"])
        recovered = self.root / "recovered chat"
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["restore", str(archive), "--into", str(recovered)]), 0)
        self.assertIn(f"AGI_DATA='{recovered}'", output.getvalue())
        after = {p.relative_to(self.data): p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        self.assertEqual(original, after)
        with self.use_conversation(recovered):
            self.assertEqual(ui_state.get_read_marker(), self.first)
            self.assertEqual(store.get_ack("codex"), self.first)
            self.assertEqual([row["id"] for row in store.read_messages(self.first) if row["kind"] == "msg"],
                             [self.next])
            self.assertEqual(store.list_todos()[0]["id"], item["id"])
            self.assertIsNone(store.list_todos()[0]["source_id"])
            self.assertEqual(membership.status("claude")["session"], "selected-session")
            self.assertFalse(membership.status("claude")["reachable"])
            self.assertFalse(json.loads((recovered / "ui/settings.json").read_text())["notifications"])
            draft = ui_state.DraftSession()
            try:
                self.assertEqual(draft.restored["draft"], "Latest draft")
            finally:
                draft.close()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["restore", str(archive), "--into", str(recovered)]), 1)

    def test_refuses_moving_snapshot_and_keeps_previous_backup(self):
        target = self.root / "existing.zip"
        target.write_bytes(b"earlier copy")
        with self.assertRaises(FileExistsError):
            backup.create(target)
        self.assertEqual(target.read_bytes(), b"earlier copy")
        target = self.root / "new.zip"
        original = backup._collect
        def changes(source):
            files = original(source)
            if changes.called:
                files.pop(Path("seq"))
            changes.called = True
            return files
        changes.called = False
        with mock.patch.object(backup, "_collect", side_effect=changes):
            with self.assertRaisesRegex(ValueError, "changed during backup"):
                backup.create(target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".agi-backup-*")), [])

    def test_invalid_backup_leaves_no_restored_directory(self):
        archive = self.root / "incomplete.zip"
        with zipfile.ZipFile(archive, "w") as contents:
            contents.writestr("backup.json", json.dumps({"format": 1, "files": {"seq": 0}}))
        destination = self.root / "restored"
        with self.assertRaisesRegex(ValueError, "does not match"):
            backup.restore(archive, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".agi-restore-*")), [])


if __name__ == "__main__":
    unittest.main()
