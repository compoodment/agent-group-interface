"""Exercise installed skill reads and receipts through the normal launcher."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = os.environ.get('AGI_SCRATCH')


class SkillReceiptsTests(unittest.TestCase):
    def test_read_records_exact_bytes_and_latest_version_by_agent(self):
        with tempfile.TemporaryDirectory(prefix='agi-skill-', dir=SCRATCH) as scratch:
            folder = Path(scratch)
            data = folder / 'data'
            source = folder / 'workspace' / 'receipt-fixture' / 'SKILL.md'
            source.parent.mkdir(parents=True)
            env = dict(os.environ, AGI_DATA=str(data), AGI_SKILLS_DIR=str(source.parents[1]))

            def skill(*args):
                return subprocess.run([str(ROOT / 'bin/agi'), 'skill', *args], env=env, check=True,
                                      capture_output=True, timeout=10).stdout

            self.assertEqual(json.loads(skill('list', '--as', 'codex')), [])
            self.assertFalse(data.exists())
            first = b'# Initial skill\r\n\r\nRead the selected file.\r\n'
            source.write_bytes(first)
            self.assertEqual(skill('read', '--as', 'codex', 'receipt-fixture'), first)
            before, = json.loads(skill('list', '--as', 'codex'))
            self.assertEqual(before['sha256'], hashlib.sha256(first).hexdigest())
            self.assertEqual(before['path'], str(source.resolve()))
            self.assertEqual(json.loads(skill('list', '--as', 'claude')), [])

            second = b'# Updated skill\n\nCheck the result.\n'
            source.write_bytes(second)
            replacement = json.loads(skill('record', '--as', 'codex', 'receipt-fixture'))
            self.assertGreater(replacement['read_at'], before['read_at'])
            self.assertEqual(replacement['sha256'], hashlib.sha256(second).hexdigest())
            self.assertEqual(json.loads(skill('list', '--as', 'codex')), [replacement])
            self.assertEqual(list((data / 'cursors').iterdir()), [])
            self.assertEqual(list((data / 'chat').iterdir()), [])

            rejected = subprocess.run([str(ROOT / 'bin/agi'), 'skill', 'record', '--as', 'codex', '../receipt-fixture'],
                                      env=env, capture_output=True, timeout=10)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(b'without a path', rejected.stderr)
