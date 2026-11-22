"""Local main-ref commit matching; all repositories here are disposable."""
from contextlib import redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import cli, gate_reminders, reminders, store


SCRATCH = os.environ.get('AGI_SCRATCH')


class GateReminderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agi-gate-', dir=SCRATCH)
        self.repo = Path(self.temporary.name) / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', '-C', str(self.repo), 'init', '-b', 'main'], check=True,
                       capture_output=True, text=True)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'user.name', 'AGI test'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'user.email', 'agi-test@example.invalid'], check=True)
        self.previous = gate_reminders.REPOSITORIES['AGI']
        gate_reminders.REPOSITORIES['AGI'] = self.repo

    def tearDown(self):
        gate_reminders.REPOSITORIES['AGI'] = self.previous
        self.temporary.cleanup()

    def git(self, *arguments):
        result = subprocess.run(['git', '-C', str(self.repo), *arguments], check=True,
                                capture_output=True, text=True)
        return result.stdout.strip()

    def commit(self, name, content):
        (self.repo / name).write_text(content)
        self.git('add', name)
        self.git('commit', '-m', f'add {name}')
        return self.git('rev-parse', 'HEAD')

    def test_exact_revision_survives_newer_main_commits_but_rebase_changes_identity(self):
        self.commit('base.txt', 'base\n')
        self.git('switch', '-c', 'candidate')
        original = self.commit('candidate.txt', 'candidate\n')
        self.git('switch', 'main')
        self.commit('main.txt', 'main\n')
        self.git('switch', '-c', 'rebased-candidate')
        rebased = self.commit('candidate.txt', 'candidate\n')
        self.git('switch', 'main')
        self.git('merge', '--ff-only', 'rebased-candidate')
        self.commit('later-gate.txt', 'later\n')
        head = gate_reminders.main_head('AGI')

        gate_reminders.validate_commit('AGI', original)
        self.assertTrue(gate_reminders.is_ancestor('AGI', rebased, head))
        self.assertFalse(gate_reminders.is_ancestor('AGI', original, head))

    def test_cli_reminder_waits_and_dispatches_once_after_the_exact_commit_lands(self):
        self.commit('base.txt', 'base')
        self.git('switch', '-c', 'candidate')
        candidate = self.commit('candidate.txt', 'ready')
        self.git('switch', 'main')
        data = Path(self.temporary.name) / 'chat'
        self.addCleanup(importlib.reload, store)
        self.enterContext(mock.patch.dict(os.environ, AGI_DATA=str(data)))
        importlib.reload(store)

        task = store.append_message('user', 'verify the package')['id']
        store.take_task('codex', task)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['remind', '--as', 'codex', str(task), '--after-gate',
                                      'AGI', candidate, '--note', 'Compare the hash']), 0)
        scheduled = json.loads(output.getvalue())
        self.assertEqual(store.list_tasks()[0]['reminder_gate'], {'project': 'AGI', 'commit': candidate})
        with mock.patch.object(gate_reminders.subprocess, 'run', wraps=subprocess.run) as probes:
            reminders.wait_limit(20)
            reminders.wait_limit(20)
            self.assertEqual(probes.call_count, 1)  # loose main ref and cached negative ancestry
        self.assertFalse(any('task_reminder_ids' in row for row in store.read_messages()))

        self.git('merge', '--ff-only', 'candidate')
        reminders.wait_limit(20)
        reminders.wait_limit(20)
        messages = [row for row in store.read_messages() if 'task_reminder_ids' in row]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['task_reminder_ids'], [scheduled['id']])
        self.assertIn('Compare the hash', messages[0]['text'])
        self.assertNotIn('reminder_gate', store.list_tasks()[0])

    def test_project_sha_shape_and_existing_commit_are_required(self):
        commit = self.commit('first.txt', 'first\n')
        self.assertEqual(gate_reminders.validate_commit('AGI', commit), commit)
        for project, revision in [('unknown', commit), ('AGI', commit[:39]), ('AGI', 'f' * 40)]:
            with self.subTest(project=project, revision=revision), self.assertRaises(ValueError):
                gate_reminders.validate_commit(project, revision)


if __name__ == '__main__':
    unittest.main()
