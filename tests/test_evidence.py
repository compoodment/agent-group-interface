"""Task suggestions reuse durable claim times and the existing evidence sources."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import evidence, gate_reminders, gates, skills, store


class TaskEvidenceTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="agi-evidence-", dir=os.environ.get("AGI_SCRATCH"),
        )))
        data = self.root / "chat-data"
        stack.enter_context(mock.patch.multiple(store, DATA_DIR=data, CHAT_DIR=data / "chat",
                                               CURSOR_DIR=data / "cursors"))
        stack.enter_context(mock.patch.dict(os.environ, AGI_SKILLS_DIR=str(self.root / "skills")))
        self.start = datetime(2026, 11, 22, 11, 0, tzinfo=timezone(timedelta(hours=1)))
        self.task = store.append_message("user", "Verify the evidence workflow.")["id"]

    def claim(self, agent="codex"):
        with mock.patch.object(store, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.start
            return store.take_task(agent, self.task)["id"]

    def read_skill(self, name, when, agent="codex"):
        path = self.root / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("# Example skill\n", encoding="utf-8")
        with mock.patch.object(skills, "datetime", wraps=datetime) as clock:
            clock.now.return_value = when.astimezone(timezone.utc)
            skills.read(agent, name)

    def test_names_come_from_current_owner_after_original_claim_time(self):
        self.read_skill("evidence-earlier", self.start - timedelta(minutes=1))
        claim_id = self.claim()
        self.read_skill("evidence-checking", self.start + timedelta(minutes=1))
        self.read_skill("evidence-other-agent", self.start + timedelta(minutes=1), "claude")
        store.set_task_doing("codex", self.task, "Checking the receipt")
        self.assertEqual(evidence.suggest("codex", self.task), {
            "claim_id": claim_id, "skills": ["evidence-checking"], "check": None,
        })
        with self.assertRaises(store.ClaimConflict):
            evidence.suggest("claude", self.task)

    def test_handoff_changes_claim_and_excludes_previous_readings(self):
        self.claim()
        self.read_skill("evidence-old-claim", self.start + timedelta(minutes=1), "claude")
        store.release_task("codex", self.task, result="Ready to hand off", check="Opened the request",
                           skills="none, because this is a handoff fixture", handoff_to="claude")
        self.start += timedelta(minutes=2)
        claim_id = self.claim("claude")
        self.assertEqual(evidence.suggest("claude", self.task), {
            "claim_id": claim_id, "skills": [], "check": None,
        })
        with self.assertRaises(store.ClaimConflict):
            evidence.suggest("codex", self.task)

    def test_only_the_explicit_exact_integrated_gate_is_suggested(self):
        self.claim()
        common = self.root / ".git"
        common.mkdir()
        commit = "a" * 40
        failed = "b" * 40
        wrong = gates.GateStatus(common, "wrong-candidate")
        wrong.update("failed", candidate=failed)
        with mock.patch.dict(gate_reminders.REPOSITORIES, AGI=self.root):
            with self.assertRaisesRegex(ValueError, "No local integration receipt"):
                evidence.suggest("codex", self.task, gate_commit=failed)
            selected = gates.GateStatus(common, "correct-candidate")
            selected.update("integrated", candidate=commit)
            check = evidence.suggest("codex", self.task, gate_commit=commit)["check"]
            self.assertEqual(check, f"AGI gate integrated {commit} (run {selected.record['run_id']})")
            self.assertIsNone(evidence.suggest("codex", self.task)["check"])

    def test_report_reference_preserves_claim_and_chat(self):
        claim_id = self.claim()
        path = self.root / "selected report.md"
        path.write_text("Report contents stay local.\n", encoding="utf-8")
        before = store.read_messages()
        self.assertEqual(evidence.suggest("codex", self.task, report=path), {
            "claim_id": claim_id, "skills": [], "check": f"Report {path.resolve()}",
        })
        self.assertEqual(store.read_messages(), before)
        self.assertEqual(store.task_claim(self.task)["claim_id"], claim_id)
        with self.assertRaisesRegex(ValueError, "existing local report"):
            evidence.suggest("codex", self.task, report=self.root / "missing")
        with self.assertRaisesRegex(ValueError, "Choose one"):
            evidence.suggest("codex", self.task, gate_commit="a" * 40, report=path)


if __name__ == "__main__":
    unittest.main()
