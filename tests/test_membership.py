"""Verify that joining a different session and releasing a task require the exact intent."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import membership, store


FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


class MembershipTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        data = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="agi-membership-", dir=os.environ.get("AGI_SCRATCH"),
        )))
        stack.enter_context(mock.patch.multiple(
            store, DATA_DIR=data, CHAT_DIR=data / "chat", CURSOR_DIR=data / "cursors",
        ))

    def test_leave_and_rejoin_preserve_cursor_and_invalidate_old_delivery(self):
        first = store.append_message("user", "First")["id"]
        store.set_ack("codex", first)
        self.assertFalse(membership.can_deliver("codex", FIRST))
        membership.observe("codex", FIRST)
        joined = membership.request_join("codex", FIRST)
        self.assertTrue(membership.can_deliver("codex", FIRST, revision=joined["revision"]))
        membership.leave("codex", FIRST)
        store.append_message("user", "While away")
        self.assertFalse(membership.can_deliver("codex", FIRST))
        self.assertTrue(membership.status("codex")["reachable"])
        resumed = membership.resume("codex")
        self.assertEqual(resumed["session"], FIRST)
        self.assertFalse(membership.can_deliver("codex", FIRST, revision=joined["revision"]))
        self.assertEqual(store.get_ack("codex"), first)
        self.assertEqual([row["text"] for row in store.read_messages(first)], ["While away"])

    def test_two_live_sessions_require_exact_confirmation(self):
        for session in (FIRST, SECOND):
            membership.observe("claude", session)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda session: membership.request_join("claude", session), (FIRST, SECOND)))
        before = membership.status("claude")
        self.assertEqual({before["session"], before["pending"]}, {FIRST, SECOND})
        self.assertTrue(membership.can_deliver("claude", before["session"]))
        self.assertFalse(membership.can_deliver("claude", before["pending"]))
        with self.assertRaisesRegex(ValueError, "not the joined"):
            membership.leave("claude", before["pending"])
        membership.decline("claude", before["pending"])
        with self.assertRaisesRegex(ValueError, "changed"):
            membership.resume("claude", before["pending"])
        membership.request_join("claude", before["pending"])
        after = membership.resume("claude", before["pending"])
        self.assertEqual(after["session"], before["pending"])
        self.assertFalse(membership.can_deliver("claude", before["session"]))

    def test_offline_session_does_not_rejoin(self):
        membership.observe("claude", FIRST, ttl=1)
        membership.request_join("claude", FIRST)
        membership.leave("claude")
        with mock.patch.object(membership.time, "time", return_value=10**12):
            with self.assertRaisesRegex(ValueError, "No selected"):
                membership.resume("claude")

    def test_user_releases_only_the_claim_shown_for_confirmation(self):
        task = store.append_message("user", "@claude Check the idea.")["id"]
        claim = store.take_task("claude", task)
        store.set_task_doing("claude", task, "Checking it")
        current = store.task_claim(task)
        self.assertEqual(current, dict(claim_id=claim["id"], owner="claude",
                                       since=claim["ts"], doing="Checking it"))
        release = store.release_task_claim(task, current["claim_id"])
        self.assertEqual(release["sender"], "system")
        self.assertEqual(release["task_revocation"]["owner"], "claude")
        self.assertEqual(store.release_task_claim(task, current["claim_id"]), release)
        self.assertEqual(store.list_tasks()[0]["state"], "released")
        self.assertIsNone(store.task_claim(task))
        replacement = store.take_task("codex", task)
        with self.assertRaisesRegex(store.ClaimConflict, "claim .* changed"):
            store.release_task_claim(task, claim["id"])
        self.assertEqual(store.task_claim(task)["claim_id"], replacement["id"])


if __name__ == "__main__":
    unittest.main()
