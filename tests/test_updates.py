"""Test installed release selection and receipt durability with isolated directories."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import claude_channel, cli, codex_mcp, store, updates


VERSION = "0.1.0-alpha.3"
ENTRY = f"## {VERSION} — 2026-11-18\n\n### Added\n\n- A test release.\n"


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="agi-updates-", dir=os.environ.get("AGI_SCRATCH"),
        )))
        self.data = root / "data"
        self.changelog = root / "CHANGELOG.md"
        self.write_changelog(ENTRY)
        stack.enter_context(mock.patch.multiple(
            store, CHECKOUT_ROOT=root, DATA_DIR=self.data, CHAT_DIR=self.data / "chat",
            CURSOR_DIR=self.data / "cursors",
        ))
        stack.enter_context(mock.patch.object(updates, "__version__", VERSION))

    def write_changelog(self, value):
        self.changelog.write_text(value, encoding="utf-8")

    def test_only_the_installed_dated_section_is_returned(self):
        self.write_changelog(
            "## Unreleased\n\nFuture notes.\n\n```markdown\n"
            f"## {VERSION} — 2025-01-01\nAn example heading.\n```\n\n"
            "## 0.1.0-alpha.30 — 2026-11-21\n\nAnother version.\n\n"
            + ENTRY + "\n## 0.1.0-alpha.2 — 2026-11-17\n\nEarlier notes.\n"
        )
        expected = {"version": VERSION, "date": "2026-11-18", "markdown": "### Added\n\n- A test release."}
        self.assertEqual(updates.current_release(), expected)
        self.assertEqual(updates.unread_release("codex"), expected)
        self.assertEqual(updates.unread_release("codex"), expected)   # Delivery alone writes nothing.
        self.assertFalse(self.data.exists())

    def test_receipts_are_independent_and_do_not_advance_chat(self):
        self.assertEqual(updates.acknowledge_release("codex", VERSION), VERSION)
        self.assertIsNone(updates.unread_release("codex"))
        self.assertIsNotNone(updates.unread_release("claude"))
        self.assertIsNotNone(updates.unread_release("user"))
        self.assertEqual(updates.acknowledge_release("claude", VERSION), VERSION)
        self.assertIsNone(updates.unread_release("claude"))
        self.assertIsNotNone(updates.unread_release("user"))
        self.assertEqual(updates.acknowledge_release("user", VERSION), VERSION)
        self.assertIsNone(updates.unread_release("user"))
        self.assertEqual(updates.current_release()["version"], VERSION)
        self.assertEqual(list(store.CURSOR_DIR.iterdir()), [])

    def test_provider_tools_acknowledge_only_the_current_reader(self):
        codex = codex_mcp.Server("codex")
        claude = claude_channel.Channel()
        def claude_notes(args):
            result, error = claude.call_tool("chat_release_notes", args)
            self.assertFalse(error, result)
            return json.loads(result)

        expected = {"release": updates.current_release()}
        self.assertEqual(codex.call("chat_release_notes", {}), expected)
        self.assertEqual(claude_notes({}), expected)
        self.assertEqual(codex.call("chat_release_notes", {"version": VERSION}), {"acked": VERSION})
        self.assertEqual(claude_notes({}), expected)
        self.assertEqual(claude_notes({"version": VERSION}), {"acked": VERSION})
        self.assertEqual(codex.call("chat_release_notes", {}), {"release": None})
        self.assertEqual(claude_notes({}), {"release": None})
        self.assertEqual(store.get_ack("claude"), 0)
        self.assertEqual(store.get_ack("codex"), 0)

    def test_cli_can_read_and_acknowledge_the_release_separately(self):
        from contextlib import redirect_stdout
        from io import StringIO

        def run(*arguments):
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(cli.main(["whatsnew", "--as", "codex", *arguments]), 0)
            return json.loads(output.getvalue())

        self.assertEqual(run(), {"release": updates.current_release()})
        self.assertEqual(run("--ack", VERSION), {"acked": VERSION})
        self.assertEqual(run(), {"release": None})
        self.assertEqual(store.get_ack("codex"), 0)

    def test_a_stale_ack_does_not_hide_a_new_release(self):
        updates.acknowledge_release("codex", VERSION)
        newer = "0.1.0-alpha.5"
        self.write_changelog(f"## {newer} — 2026-11-22\n\nA later release.\n\n" + ENTRY)
        with mock.patch.object(updates, "__version__", newer):
            self.assertEqual(updates.unread_release("codex")["version"], newer)
            with self.assertRaisesRegex(ValueError, "installed version"):
                updates.acknowledge_release("codex", VERSION)
            self.assertEqual(updates.unread_release("codex")["version"], newer)
            updates.acknowledge_release("codex", newer)
            self.assertIsNone(updates.unread_release("codex"))

    def test_missing_or_invalid_entries_never_create_receipts(self):
        invalid = (
            "## Unreleased\n\nA draft.\n",
            ENTRY + "\n" + ENTRY,
            ENTRY + f"\n## {VERSION}\n\nAn undated duplicate.\n",
            ENTRY.replace("2026-11-18", "2026-02-30"),
            f"## {VERSION} — 2026-11-18\n\n",
            f"```markdown\n{ENTRY}```\n",
        )
        for contents in invalid:
            with self.subTest(contents=contents):
                self.write_changelog(contents)
                self.assertIsNone(updates.current_release())
                self.assertIsNone(updates.unread_release("user"))
                with self.assertRaisesRegex(ValueError, "no valid changelog"):
                    updates.acknowledge_release("user", VERSION)
        self.changelog.unlink()
        self.assertIsNone(updates.current_release())
        with self.assertRaisesRegex(ValueError, "no valid changelog"):
            updates.acknowledge_release("user", VERSION)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            updates.acknowledge_release("../user", VERSION)
        self.assertFalse(self.data.exists())

    def test_full_disk_before_publishing_keeps_the_entry_unread(self):
        with mock.patch.object(store, "_atomic_json", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                updates.acknowledge_release("user", VERSION)
        self.assertIsNotNone(updates.unread_release("user"))
        updates.acknowledge_release("user", VERSION)
        self.assertIsNone(updates.unread_release("user"))

    def test_retry_syncs_the_directory_after_uncertain_publication(self):
        sync = store._sync_directory

        def fail_receipt_sync(path):
            if path == self.data / "updates":
                raise OSError(errno.EIO, "receipt directory sync failed")
            sync(path)

        with mock.patch.object(store, "_sync_directory", side_effect=fail_receipt_sync):
            with self.assertRaises(OSError):
                updates.acknowledge_release("user", VERSION)
        with mock.patch.object(store, "_atomic_json") as write:
            with mock.patch.object(store, "_sync_directory", wraps=sync) as retry:
                self.assertEqual(updates.acknowledge_release("user", VERSION), VERSION)
            write.assert_not_called()
        retry.assert_any_call(self.data / "updates")
        self.assertIsNone(updates.unread_release("user"))

    def test_concurrent_acknowledgments_publish_one_receipt(self):
        def acknowledge(reader):
            return updates.acknowledge_release(reader, VERSION)

        with mock.patch.object(store, "_atomic_json", wraps=store._atomic_json) as write:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(acknowledge, ("user", "user"))), [VERSION, VERSION])
        write.assert_called_once()
        self.assertIsNone(updates.unread_release("user"))


if __name__ == "__main__":
    unittest.main()
