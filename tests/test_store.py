"""Store regression tests. Every test uses a throwaway data root on Storage."""
import concurrent.futures
from datetime import datetime
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from agi import store

SCRATCH = os.environ.get('AGI_SCRATCH')


TASK_RESULT = "Reviewed the fixture"
TASK_CHECK = "Replayed the fixture"
TASK_SKILLS = "none, because this is a synthetic fixture"


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='agi-test-', dir=SCRATCH)
        self.environment = mock.patch.dict(os.environ, AGI_DATA=self.directory.name)
        self.environment.start()
        importlib.reload(store)

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def user(self, text='hello'):
        return store.append_message('user', text)


class TodoTests(StoreFixture):
    def add_item(self, **options):
        source = store.append_message('claude', 'I will try the Properties test after release.')
        return store.add_todo('claude', source['id'], 'ArtLab', 'Properties test', 'claude',
                              'Try the pinned UI harness after release.', **options)

    def test_conversation_deadline_waits_for_a_speaker_and_cues_them_once(self):
        when = 1_796_000_000
        item = self.add_item(due_at=when, remind_next_actor=True)
        self.assertIsNone(store.next_reminder_deadline(when))
        self.assertEqual(store.dispatch_due_reminders(when), [])
        item = store.update_todo('claude', item['id'], item['revision'], next_actor='codex')
        self.assertEqual(store.next_reminder_deadline(when), when)
        notice, = store.dispatch_due_reminders(when)
        self.assertTrue(store.addressed_to(notice, 'codex'))
        self.assertFalse(store.addressed_to(notice, 'claude'))
        self.assertEqual(notice['todo_check']['recipient'], 'codex')
        self.assertEqual(notice['todo_check']['owner'], 'claude')
        self.assertIn(item['next_action'], notice['text'])
        importlib.reload(store)
        stored, = store.list_todos()
        self.assertEqual(stored, item)
        store.update_todo('claude', item['id'], item['revision'], next_actor='claude')
        self.assertEqual(store.dispatch_due_reminders(when), [])

    def test_answer_to_a_waiting_todo_cues_only_its_current_owner_once(self):
        item = self.add_item(next_actor='user')
        store.set_policy('paused')
        self.user(f"↪ #{item['source_id']} Start with Linux.")
        answer = self.user(f"↪ #{item['source_id']} Use the larger Linux fixture.")
        self.assertEqual(store.dispatch_todo_answers(), [])

        importlib.reload(store)
        store.resume_policy()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: store.dispatch_todo_answers(), range(2)))
        cue, = [row for notices in results for row in notices]
        self.assertEqual(cue['todo_answer']['answer'], answer['id'])
        self.assertIn(f"Is T{item['id']} still waiting on them?", cue['text'])
        self.assertTrue(store.addressed_to(cue, 'claude'))
        self.assertFalse(store.addressed_to(cue, 'codex'))
        self.assertFalse(store.addressed_to(cue, 'user'))
        self.assertEqual(store.list_todos(), [item])

        self.user(f"↪ #{item['source_id']} One more detail.")
        updated = store.update_todo('claude', item['id'], item['revision'], next_actor='claude')
        self.assertEqual(store.dispatch_todo_answers(), [])
        self.user(f"↪ #{item['source_id']} This item now waits on Claude.")
        self.assertEqual(store.dispatch_todo_answers(), [])
        self.assertEqual(store.list_todos(), [updated])

    def test_following_checks_wait_for_a_claim_and_keep_the_exact_gate(self):
        request = self.user('Check the candidate before closing this request')['id']
        when = 1_796_000_000
        commit, head = 'a' * 40, 'b' * 40

        def following(title, **fields):
            return store.add_todo('codex', request, 'AGI', title, 'codex', 'Inspect the candidate',
                                  follow_request=True, **fields)

        with self.assertRaisesRegex(ValueError, 'claimed by its owner'):
            following('Timed replay', due_at=when)
        store.take_task('codex', request)
        timed = following('Timed replay', due_at=when)
        with mock.patch.object(store.gate_reminders, 'validate_commit', return_value=commit):
            gate = following('Exact revision', wait_for=f'gate:AGI:{commit}')
        with self.assertRaisesRegex(ValueError, 'independent to-do'):
            store.defer_task('codex', request, [timed['id']], result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        store.release_task('codex', request, handoff_to='claude', result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertIsNone(store.next_reminder_deadline(when))
        self.assertEqual(store.dispatch_due_reminders(when), [])
        with mock.patch.object(store.gate_reminders, 'main_head') as resolve:
            self.assertEqual(store.dispatch_todo_waiters(), [])
            resolve.assert_not_called()

        store.take_task('claude', request)
        self.assertEqual({item['owner'] for item in store.list_todos()}, {'claude'})
        notice, = store.dispatch_due_reminders(when)
        self.assertEqual(notice['todo_check']['id'], timed['id'])
        self.assertTrue(store.addressed_to(notice, 'claude'))
        self.assertFalse(store.addressed_to(notice, 'codex'))
        with (mock.patch.object(store.gate_reminders, 'main_head', return_value=head),
              mock.patch.object(store.gate_reminders, 'is_ancestor', return_value=True) as reaches):
            ready, = store.dispatch_todo_waiters()
            reaches.assert_called_once_with('AGI', commit, head)
            self.assertEqual(ready['todo_event']['id'], gate['id'])
            self.assertEqual(ready['todo_event']['actor'], 'claude')
        importlib.reload(store)
        self.assertEqual(store.dispatch_due_reminders(when), [])
        self.assertEqual(store.dispatch_todo_waiters(), [])

    def test_finishing_request_cancels_linked_checks_without_touching_ordinary_todos(self):
        request = self.user('Check before release')['id']
        store.take_task('codex', request)
        when = 1_796_000_000
        following = store.add_todo('codex', request, 'AGI', 'Linked check', 'codex', 'Check it',
                                   due_at=when, follow_request=True)
        ordinary = store.add_todo('codex', request, 'AGI', 'Independent check', 'codex', 'Check later', due_at=when)
        store.release_task('codex', request, done=True, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        importlib.reload(store)
        self.assertEqual(store.list_todos(), [ordinary])
        rows = {row['id']: row for row in store.list_todos(include_done=True)}
        self.assertEqual(rows[following['id']]['result'], TASK_RESULT)
        notice, = store.dispatch_due_reminders(when)
        self.assertEqual(notice['todo_check']['id'], ordinary['id'])

    def test_todo_persists_source_deadline_and_outcome(self):
        item = self.add_item(due_at=1_796_000_000, reference='docs/Checklist.md')
        self.assertEqual(item['project'], 'Artlab')
        self.assertEqual(item['created_at'], item['updated_at'])
        self.assertIsNone(item['next_actor'])
        updated = store.update_todo('claude', item['id'], item['revision'],
                                    next_actor='user', next_action='Choose the first test case.')
        self.assertEqual(updated['created_at'], item['created_at'])
        self.assertEqual(updated['source_id'], item['source_id'])
        self.assertGreater(updated['revision'], item['revision'])
        done = store.complete_todo('claude', item['id'], updated['revision'], 'Passed; see Reports/ui-test.md')
        self.assertEqual(done['due_at'], item['due_at'])
        self.assertIsNone(done['next_actor'])
        importlib.reload(store)
        self.assertEqual(store.list_todos(), [])
        self.assertEqual(store.list_todos(include_done=True), [done])

    def test_timed_todo_wakes_the_current_owner_without_changing_the_item(self):
        when = time.time() + 60
        prerequisite = self.add_item()
        item = self.add_item(due_at=when, wait_for=f"todo:{prerequisite['id']}")
        current = store.update_todo('user', item['id'], item['revision'],
                                    owner='codex', title='Check @claude', next_action='Check again.\n@all text')
        self.assertEqual(store.next_reminder_deadline(now=when - 1), when)
        self.assertEqual(store.dispatch_due_reminders(now=when - 1), [])
        notice, = store.dispatch_due_reminders(now=when)
        self.assertEqual(notice["todo_check"], dict(id=item["id"], schedule_revision=item["revision"],
                                                  due_at=when, owner="codex"))
        self.assertIn("> Check again.\n> @all text", notice["text"])
        self.assertTrue(store.addressed_to(notice, "codex"))
        self.assertFalse(store.addressed_to(notice, "claude"))
        self.assertFalse(store.addressed_to(notice, "user"))
        self.assertEqual(store.list_todos(), [prerequisite, current])
        self.assertIsNone(store.next_reminder_deadline(now=when))

        repeated = store.update_todo('user', item['id'], current['revision'], due_at=when, owner='claude')
        self.assertEqual(store.dispatch_due_reminders(now=when), [])
        rescheduled = store.update_todo('claude', item['id'], repeated['revision'], due_at=when + 60)
        self.assertEqual(store.dispatch_due_reminders(now=when + 59), [])
        notice, = store.dispatch_due_reminders(now=when + 60)
        self.assertEqual(notice["todo_check"]["schedule_revision"], rescheduled["revision"])
        self.assertTrue(store.addressed_to(notice, "claude"))
        self.assertFalse(store.addressed_to(notice, "codex"))

    def test_timed_todo_clear_complete_and_resume(self):
        when = time.time()
        cleared = self.add_item(due_at=when)
        completed = self.add_item(due_at=when)
        pending = self.add_item(due_at=when)
        store.update_todo('claude', cleared['id'], cleared['revision'], due_at=None)
        store.complete_todo('claude', completed['id'], completed['revision'], 'Checked.')
        store.update_todo('user', pending['id'], pending['revision'], owner='user')

        store.set_policy("paused")
        self.assertIsNone(store.next_reminder_deadline(now=when))
        self.assertEqual(store.dispatch_due_reminders(now=when), [])
        store.resume_policy()
        notice, = store.dispatch_due_reminders(now=when)
        self.assertEqual(notice["todo_check"]["id"], pending["id"])
        self.assertTrue(store.addressed_to(notice, 'user'))
        self.assertFalse(store.addressed_to(notice, 'claude'))

    def test_timed_todo_interrupted_dispatch_retries_and_concurrent_listeners_do_not_duplicate(self):
        when = time.time()
        item = self.add_item(due_at=when)
        write = store._write_record
        with mock.patch.object(store, "_write_record", side_effect=OSError("disk full before append")):
            with self.assertRaisesRegex(OSError, "before append"):
                store.dispatch_due_reminders(now=when)
        self.assertEqual(store.next_reminder_deadline(now=when), when)

        def lose_receipt(*args, **kwargs):
            record = write(*args, **kwargs)
            if kwargs.get("todo_check"):
                raise OSError("connection lost after durable append")
            return record

        with mock.patch.object(store, "_write_record", side_effect=lose_receipt):
            with self.assertRaisesRegex(OSError, "after durable append"):
                store.dispatch_due_reminders(now=when)
        importlib.reload(store)
        self.assertEqual(store.dispatch_due_reminders(now=when), [])
        records = [record for record in store.read_messages() if "todo_check" in record]
        self.assertEqual([record["todo_check"]["id"] for record in records], [item["id"]])

        cleared = store.update_todo('claude', item['id'], item['revision'], due_at=None)
        rescheduled = store.update_todo('claude', item['id'], cleared['revision'], due_at=when)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: store.dispatch_due_reminders(now=when), range(2)))
        notice, = (record for batch in results for record in batch)
        self.assertEqual(notice["todo_check"]["schedule_revision"], rescheduled["revision"])
        self.assertEqual(store.dispatch_due_reminders(now=when), [])

    def test_timed_todo_replay_rejects_a_false_receipt(self):
        when = time.time()
        item = self.add_item(due_at=when)
        notice, = store.dispatch_due_reminders(now=when)
        rows = store.read_messages()
        invalid = dict(notice, todo_check=dict(notice["todo_check"], due_at=when + 1))
        with self.assertRaisesRegex(ValueError, f"Invalid timed check for T{item['id']}"):
            store._summary(rows[:-1] + [invalid])
        malformed = dict(notice, todo_check=dict(notice["todo_check"], schedule_revision=notice["id"]))
        with self.assertRaisesRegex(ValueError, "schedule must precede"):
            store._parse_record(json.dumps(malformed).encode(), "malformed check")

    def test_timed_todo_preserves_a_partial_tail_and_a_stale_sequence(self):
        when = time.time()
        item = self.add_item(due_at=when)
        path = next(store.CHAT_DIR.glob("*.jsonl"))
        suffix = b'{"todo_check":{"id":'
        with path.open("ab") as stream:
            stream.write(suffix)
        write = store._atomic_write

        def fail_sequence(path, contents):
            if path.name == "seq":
                raise OSError("interrupted after journal append")
            return write(path, contents)

        with mock.patch.object(store, "_atomic_write", side_effect=fail_sequence):
            notice, = store.dispatch_due_reminders(now=when)
        preserved, = (store.DATA_DIR / "recovery").glob("*.partial")
        self.assertEqual(preserved.read_bytes(), suffix)
        self.assertEqual(int((store.DATA_DIR / "seq").read_text()), item["revision"])
        importlib.reload(store)
        self.assertEqual(store.dispatch_due_reminders(now=when), [])
        self.assertEqual(store.read_messages()[-1], notice)
        self.assertGreater(self.user()["id"], notice["id"])

    def test_todo_retries_return_receipt_before_later_updates(self):
        item = self.add_item()
        request = 'f' * 32
        updated = store.update_todo('claude', item['id'], item['revision'], request_id=request, next_actor='claude')
        newer = store.update_todo('claude', item['id'], updated['revision'], next_actor='user')
        retried = store.update_todo('claude', item['id'], item['revision'], request_id=request, next_actor='claude')
        self.assertEqual(retried, updated)
        self.assertEqual(store.list_todos(), [newer])
        with self.assertRaisesRegex(ValueError, 'already used'):
            store.update_todo('claude', item['id'], item['revision'], request_id=request, next_actor='user')

    def test_todo_prerequisite_wakes_the_owner_once_and_replays_the_ready_row(self):
        first = self.add_item()
        source = self.user()['id']
        second = store.add_todo('codex', source, 'AGI', 'Follow up @claude', 'codex', 'Check after the first test.',
                                wait_for=f"todo:{first['id']}", next_actor='codex')
        self.assertIsNone(second['next_actor'])
        self.assertEqual(store.dispatch_todo_waiters(), [])
        store.complete_todo('claude', first['id'], first['revision'], 'Passed.')

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            delivered = list(executor.map(lambda _: store.dispatch_todo_waiters(), range(2)))
        notices = [record for batch in delivered for record in batch]
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertTrue(store.addressed_to(notice, 'codex'))
        self.assertFalse(store.addressed_to(notice, 'claude'))

        importlib.reload(store)
        ready = store.list_todos()[0]
        self.assertEqual((ready['next_actor'], ready['revision']), ('codex', notice['id']))
        self.assertNotIn('wait_for', ready)
        self.assertEqual(store.dispatch_todo_waiters(), [])
        with self.assertRaisesRegex(ValueError, 'Reload the list'):
            store.complete_todo('codex', second['id'], second['revision'], 'Stale revision.')
        store.complete_todo('codex', ready['id'], ready['revision'], 'Verified.')
        self.assertEqual(store.list_todos(), [])

    def test_todo_wait_validates_references_and_cancellation(self):
        first = self.add_item()
        second = self.add_item(wait_for=f"todo:{first['id']}")
        with self.assertRaisesRegex(ValueError, 'cannot wait for itself'):
            store.update_todo('claude', first['id'], first['revision'], wait_for=f"todo:{second['id']}")
        with self.assertRaisesRegex(ValueError, 'No prerequisite to-do'):
            store.update_todo('claude', second['id'], second['revision'], wait_for='todo:999')
        with self.assertRaisesRegex(ValueError, 'Clear the prerequisite'):
            store.update_todo('claude', second['id'], second['revision'], next_actor='claude')
        resumed = store.update_todo('claude', second['id'], second['revision'],
                                    wait_for=None, next_actor='claude', request_id='7' * 32)
        self.assertEqual(store.update_todo('claude', second['id'], second['revision'], wait_for=None,
                                          next_actor='claude', request_id='7' * 32), resumed)
        self.assertNotIn('wait_for', resumed)
        self.assertEqual(resumed['next_actor'], 'claude')
        store.complete_todo('claude', first['id'], first['revision'], 'Passed.')
        self.assertEqual(store.dispatch_todo_waiters(), [])

    def test_todo_gate_revalidates_revision_and_release_requires_a_published_tag(self):
        sha = 'a' * 40
        with mock.patch.object(store.gate_reminders, 'validate_commit', return_value=sha):
            item = self.add_item(wait_for=f'gate:AGI:{sha}')

        def closed_during_probe(project, commit, head):
            self.assertEqual((project, commit, head), ('AGI', sha, 'b' * 40))
            store.complete_todo('claude', item['id'], item['revision'], 'Completed before the gate.')
            return True

        with mock.patch.object(store.gate_reminders, 'main_head', return_value='b' * 40):
            with mock.patch.object(store.gate_reminders, 'is_ancestor', side_effect=closed_during_probe):
                self.assertEqual(store.dispatch_todo_waiters(), [])
            with mock.patch.object(store.gate_reminders, 'validate_commit', return_value=sha):
                self.add_item(wait_for=f'gate:AGI:{sha}')
            with mock.patch.object(store.gate_reminders, 'is_ancestor', return_value=True):
                self.assertEqual(len(store.dispatch_todo_waiters()), 1)

        release = self.add_item(wait_for='release:ArtLab:v0.8.0-alpha.1')
        for interval, draft, tag, published_at, ready in [
            (1, True, 'v0.8.0-alpha.1', None, False),
            (2, False, 'v0.7.0-alpha.1', '2026-11-21T05:00:00Z', False),
            (3, False, 'v0.8.0-alpha.1', '2026-11-22T05:00:00Z', True),
        ]:
            payload = json.dumps(dict(draft=draft, tag_name=tag, published_at=published_at)).encode()
            with mock.patch.object(store.release_tags, 'urlopen', return_value=io.BytesIO(payload)) as get, \
                    mock.patch.object(store.release_tags.time, 'monotonic', return_value=interval * 120):
                notices = store.dispatch_todo_waiters()
                self.assertEqual(bool(notices), ready)
                request = get.call_args.args[0]
                self.assertEqual(request.full_url, 'https://api.github.com/repos/compoodment/Artlab/'
                                                  'releases/tags/v0.8.0-alpha.1')
                self.assertIsNone(request.get_header('Authorization'))
                if ready:
                    self.assertTrue(notices[0]['todo_ready'])
                    self.assertEqual(notices[0]['todo_event']['id'], release['id'])
        self.assertEqual(store.dispatch_todo_waiters(), [])

    def test_todo_edit_has_one_winner_and_requires_owner(self):
        item = self.add_item()
        with self.assertRaisesRegex(ValueError, 'owned by claude'):
            store.complete_todo('codex', item['id'], item['revision'], 'Cancelled')

        def change(title):
            return store.update_todo('claude', item['id'], item['revision'], title=title)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(change, title) for title in ('Clip test', 'Properties panel test')]
        self.assertEqual(sum(future.exception() is None for future in futures), 1)
        failure = next(future.exception() for future in futures if future.exception() is not None)
        self.assertIsInstance(failure, ValueError)
        self.assertIn('Reload the list', str(failure))

    def test_todo_protects_source_and_cached_events(self):
        with self.assertRaisesRegex(ValueError, 'participant'):
            store.add_todo('codex', 99, 'AGI', 'Test', 'codex', 'Check the source.')
        item = self.add_item()
        with self.assertRaisesRegex(ValueError, 'Unix seconds'):
            store.update_todo('claude', item['id'], item['revision'], due_at=float('nan'))
        event = store.read_messages()[-1]['todo_event']
        event['changes']['title'] = 'Caller mutation'
        self.assertEqual(store.list_todos(), [item])
        self.assertEqual(store.guard_status()['agent_streak'], 1)

    def test_next_todo_is_one_per_owner_and_keeps_the_original_trigger(self):
        source = self.user()['id']
        first = store.add_todo('codex', source, 'AGI', 'First', 'codex', 'Review when ready.')
        second = store.add_todo('codex', source, 'AGI', 'Second', 'codex', 'Check Monday.', next_actor='codex')
        third = store.add_todo('claude', source, 'AGI', 'Third', 'claude', 'Continue after the gate.')
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            store.set_next_todo('claude', first['id'])
        chosen = store.set_next_todo('user', first['id'])
        self.assertEqual(chosen, dict(first, is_next=True))
        store.set_next_todo('codex', second['id'])
        store.set_next_todo('claude', third['id'])
        importlib.reload(store)
        self.assertEqual([item['id'] for item in store.list_todos() if item['is_next']], [second['id'], third['id']])
        store.set_next_todo('user', owner='claude')
        self.assertEqual([item['id'] for item in store.list_todos() if item['is_next']], [second['id']])

    def test_next_todo_clears_on_transfer_or_completion(self):
        item = self.add_item()
        store.set_next_todo('claude', item['id'])
        moved = store.update_todo('claude', item['id'], item['revision'], owner='codex')
        self.assertFalse(moved['is_next'])
        restored = store.update_todo('codex', item['id'], moved['revision'], owner='claude')
        self.assertFalse(restored['is_next'])
        store.set_next_todo('claude', item['id'])
        done = store.complete_todo('claude', item['id'], restored['revision'], 'Properties passed.')
        self.assertFalse(done['is_next'])
        self.assertEqual(store.list_todos(include_done=True), [done])
        with self.assertRaisesRegex(ValueError, 'No open to-do'):
            store.set_next_todo('claude', item['id'])

    def test_next_todo_retry_preserves_a_later_choice(self):
        first = self.add_item()
        second = self.add_item()
        request_id = 'a' * 32
        receipt = store.set_next_todo('claude', first['id'], request_id=request_id)
        store.set_next_todo('claude', second['id'])
        self.assertEqual(store.set_next_todo('claude', first['id'], request_id=request_id), receipt)
        self.assertEqual([item['id'] for item in store.list_todos() if item['is_next']], [second['id']])
        with self.assertRaisesRegex(ValueError, 'already used'):
            store.set_next_todo('claude', second['id'], request_id=request_id)


class DecisionTests(StoreFixture):
    def ask(self, request_id=None):
        prompt = {'project': 'AGI', 'choices': ['1a', '1b', '2a', '2b', '3a', '3b']}
        return store.append_message('codex', 'Choose the review, timing and record format.',
                                    decision_prompt=prompt, request_id=request_id)

    def test_direct_reply_records_selections_and_retains_original_qualification(self):
        question = self.ask()
        body = '1. a yes when the same task is shared.\n2. a\n3. a'
        answer = self.user(f'↪ #{question["id"]} {body}')
        importlib.reload(store)
        row, = store.list_decisions()
        self.assertEqual(row['question_id'], question['id'])
        self.assertEqual(row['selections'], {
            '1': {'choice': '1a', 'answer_id': answer['id'], 'qualified': True},
            '2': {'choice': '2a', 'answer_id': answer['id'], 'qualified': False},
            '3': {'choice': '3a', 'answer_id': answer['id'], 'qualified': False},
        })
        self.assertTrue(store.read_messages(answer['id'] - 1)[0]['text'].endswith(body))

    def test_ambiguity_cannot_replace_a_prior_pick(self):
        question = self.ask()
        reply = f'↪ #{question["id"]} '
        store.append_message('claude', reply + '1b')
        self.user('1b')
        self.assertEqual(store.list_decisions()[0]['selections'], {})
        first = self.user(reply + '1a 2b')
        uncertain = self.user(reply + '1. a or b')
        row, = store.list_decisions()
        self.assertEqual(row['review_answer_id'], uncertain['id'])
        self.assertEqual(row['selections']['1']['answer_id'], first['id'])
        corrected = self.user(reply + '1b')
        row, = store.list_decisions()
        self.assertIsNone(row['review_answer_id'])
        self.assertEqual(row['selections']['1']['answer_id'], corrected['id'])
        self.assertEqual(row['selections']['2']['choice'], '2b')

    def test_marked_prompt_shares_posting_policy_and_request_receipt(self):
        request_id = 'f' * 32
        question = self.ask(request_id)
        store.set_policy('paused')
        self.assertEqual(self.ask(request_id), question)
        with self.assertRaisesRegex(ValueError, 'different message content'):
            store.append_message('codex', question['text'], request_id=request_id)
        with self.assertRaises(store.GuardBlocked):
            self.ask()
        self.assertEqual(len(store.list_decisions()), 1)


class StoreTests(StoreFixture):
    def test_guard_four_and_once_per_episode(self):
        store.set_policy('wait')
        self.user()
        for index in range(4):
            store.append_message(store.AGENTS[index % 2], str(index))
        for _ in range(3):
            with self.assertRaises(store.GuardBlocked):
                store.append_message('codex', 'blocked')
        notices = [m for m in store.read_messages() if m['text'].startswith(store._GUARD_NOTICE)]
        self.assertEqual(len(notices), 1)
        self.assertTrue(store.guard_status()['blocked'])
        self.user('reset')
        self.assertEqual(store.guard_status()['agent_streak'], 0)

    def test_cache_unchanged_and_independent_results(self):
        msg = self.user()
        with mock.patch.object(store, '_read_file', wraps=store._read_file) as read:
            result = store.read_messages()
            result[0]['text'] = 'changed outside'
            store.read_messages()
            self.assertEqual(read.call_count, 1)
        self.assertEqual(store.read_messages()[0], msg)

    def test_append_reads_only_new_bytes(self):
        self.user()
        store.read_messages()
        self.user('next')
        with mock.patch.object(store, '_read_file', wraps=store._read_file) as read:
            self.assertEqual(len(store.read_messages()), 2)
            self.assertGreater(read.call_args.args[2], 0)

    def test_replacement_truncation_same_size_and_removal(self):
        self.user('aaaa')
        old = store.read_messages()[0]
        path = next(store.CHAT_DIR.glob('*.jsonl'))
        text = path.read_text().replace('aaaa', 'bbbb')
        path.write_text(text)
        self.assertEqual(store.read_messages()[0]['text'], 'bbbb')
        replacement = path.with_suffix('.new')
        replacement.write_text(text.replace('bbbb', 'cccc'))
        replacement.replace(path)
        self.assertEqual(store.read_messages()[0]['text'], 'cccc')
        path.write_text('')
        self.assertEqual(store.last_id(), 0)
        path.unlink()
        self.assertEqual(store.read_messages(), [])

    def test_partial_tail_recovery(self):
        msg = self.user()
        path = next(store.CHAT_DIR.glob('*.jsonl'))
        with path.open('ab') as f:
            f.write(b'{"id":999')
        self.assertEqual(store.read_messages(), [msg])
        self.user('after recovery')
        self.assertEqual(len(store.read_messages()), 2)
        partials = list((store.DATA_DIR/'recovery').glob('*.partial'))
        self.assertEqual(partials[0].read_bytes(), b'{"id":999')

    def test_completed_partial_line_becomes_visible(self):
        self.user()
        path = next(store.CHAT_DIR.glob('*.jsonl'))
        m = {'id':2, 'ts':'2026-11-16', 'sender':'user', 'kind':'msg', 'text':'next'}
        data = json.dumps(m).encode()
        with path.open('ab') as f:f.write(data[:10])
        self.assertEqual(store.last_id(),1)
        with path.open('ab') as f:f.write(data[10:]+b'\n')
        self.assertEqual(store.last_id(),2)

    def test_duplicate_and_invalid_committed_record_rejected(self):
        self.user()
        path = next(store.CHAT_DIR.glob('*.jsonl'))
        with path.open('ab') as f:f.write(path.read_bytes())
        with self.assertRaisesRegex(ValueError,'duplicate'):store.read_messages()
        path.write_bytes(b'not json\n')
        with self.assertRaisesRegex(ValueError,'Invalid log'):store.read_messages()

    def test_ack_bounds_and_monotonicity(self):
        first=self.user()['id'];second=self.user()['id']
        store.set_ack('codex',second)
        with self.assertRaises(ValueError):store.set_ack('codex',first)
        with self.assertRaises(ValueError):store.set_ack('codex',second+1)
        self.assertEqual(store.get_ack('codex'),second)

    def test_concurrent_process_writers(self):
        code = 'from agi import store; [store.append_message("user", str(i)) for i in range(15)]'
        env=dict(os.environ, PYTHONPATH=str(Path(store.__file__).resolve().parents[1]))
        processes=[subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL) for _ in range(4)]
        for process in processes:self.assertEqual(process.wait(timeout=20),0)
        ids=[m['id'] for m in store.read_messages()]
        self.assertEqual(ids,list(range(1,61)))

    def test_quoted_and_partial_mentions_do_not_assign_a_task(self):
        examples = ['what does `@claude` mean?', '``@claude`` is code',
                    '```text\n@claude\n```', '~~~\n@claude\n~~~~',
                    '> @claude old quotation', '    @claude code',
                    'mail@claude.example', '@claude-old', r'\@claude literal']
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(store.mentioned_agents(text), set())
                request = store.append_message('user', text)
                claim = store.take_task('codex', request['id'])
                self.assertEqual(claim['sender'], 'codex')
                store.release_task('codex', request['id'], result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(store.mentioned_agents('@ClAuDe please act'), {'claude'})
        self.assertEqual(store.mentioned_agents('@all please act'), {'claude', 'codex'})
        self.assertEqual(store.mentioned_agents('`@claude` example; @codex act'), {'codex'})
        self.assertEqual(store.mentioned_agents('```\n@codex unclosed'), set())

    def test_inline_code_uses_whole_backtick_runs(self):
        self.assertEqual(store.mentioned_agents('prefix `` @codex ` text ``'), set())
        self.assertEqual(store.mentioned_agents('@co`example`dex'), set())
        # One long unmatched run must remain prose without regex backtracking.
        self.assertEqual(store.mentioned_agents('prefix '+ '`'*16000 + ' @codex'), {'codex'})
        self.assertEqual(store.mentioned_agents('prefix ` example @claude'), {'claude'})

    def test_addressed_to_recognizes_mentions_and_exact_reply_targets(self):
        user = self.user('question')
        codex = store.append_message('codex', 'answer')
        claim = store.append_message('codex', 'review.py', 'claim')
        message = dict(sender='claude', kind='msg')
        for text, recipient, expected in (
            ('@CoDeX please check', 'codex', True),
            ('@all please check', 'claude', False),
            ('`@codex` is an example', 'codex', False),
            ('@you this is ready', store.USER, True),
            (f"↪ #{codex['id']} response", 'codex', True),
            (f"↪ #{user['id']} response", 'codex', False),
            (f"↪ #{claim['id']} response", 'codex', False),
            ('↪ #0 response', 'codex', False),
            (f"↪ #{codex['id']}suffix", 'codex', False),
            ('↪ #1234567890123 response', 'codex', False),
        ):
            with self.subTest(text=text, recipient=recipient):
                self.assertIs(store.addressed_to(dict(message, text=text), recipient), expected)
        reply = dict(message, text=f"↪ #{user['id']} response")
        self.assertTrue(store.addressed_to(reply, store.USER, lookup={user['id']: user}.get))
        self.assertFalse(store.addressed_to(reply, store.USER, lookup={}.get))
        self.assertFalse(store.addressed_to(dict(reply, kind='claim'), store.USER))

    def test_task_race_and_assignment(self):
        task=self.user('shared request')['id']
        def take(agent):
            try:store.take_task(agent, task, doing=f"{agent} checks the request");return True
            except store.ClaimConflict:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            wins=list(pool.map(take,store.AGENTS))
        self.assertEqual(sum(wins),1)
        targeted=self.user('@CoDeX only')['id']
        with self.assertRaises(store.ClaimConflict):store.take_task('claude',targeted)
        store.take_task('codex',targeted)

    def test_current_task_action_follows_its_owner(self):
        task = self.user("Rename the GitHub repositories")["id"]
        claim = store.take_task("claude", task, doing="Checking the repository names")
        self.assertEqual(claim["doing"], "Checking the repository names")
        self.assertEqual(store.list_tasks()[0]["doing"], claim["doing"])
        with self.assertRaises(store.ClaimConflict):
            store.set_task_doing("codex", task, "Starting the rename")
        with self.assertRaises(ValueError):
            store.set_task_doing("claude", task, "One line\nAnother line")

        updated = store.set_task_doing("claude", task, "Renaming the GitHub repositories")
        row = store.list_tasks()[0]
        self.assertEqual(row["doing"], updated["doing"])
        self.assertEqual((row["event_id"], row["event_ts"]), (claim["id"], claim["ts"]))
        self.assertEqual(store.set_task_doing("claude", task, updated["doing"])["id"], updated["id"])
        self.assertEqual(store.take_task("claude", task)["doing"], updated["doing"])
        store.release_task("claude", task, handoff_to="codex", result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertNotIn("doing", store.list_tasks()[0])
        store.take_task("codex", task, doing="Checking the links")
        store.release_task("codex", task, done=True, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertNotIn("doing", store.list_tasks()[0])

    def test_task_handoff_stays_open_and_completion_closes_it(self):
        request = self.user("repair this")
        task = request["id"]
        store.take_task("codex", task)
        release = store.release_task("codex", task, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(store.list_tasks()[0]["state"], "released")
        self.assertIsNone(store.list_tasks()[0]["owner"])
        self.assertNotIn("note", store.list_tasks()[0])
        self.assertNotIn("done", release)
        store.take_task("claude", task)
        with self.assertRaises(store.ClaimConflict):
            store.release_task("codex", task, done=True, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        completion = store.release_task("claude", task, done=True, result="fixed in abc123", check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(completion["kind"], "release")
        self.assertTrue(completion["done"])
        self.assertEqual(store.list_tasks(), [{
            "id": task, "text": request["text"], "ts": request["ts"], "state": "done",
            "owner": "claude", "result": "fixed in abc123", "event_id": completion["id"],
            "event_ts": completion["ts"],                # when it ended, for /tasks' ages
            "helpers": [], "helper_todos": {},
            "check": TASK_CHECK, "skills": TASK_SKILLS,
        }])
        self.assertEqual(store.active_claims(), {})
        self.assertEqual(store.release_task("claude", task, done=True, result="fixed in abc123", check=TASK_CHECK, skills=TASK_SKILLS), completion)
        self.assertEqual(store.last_id(), completion["id"])
        with self.assertRaises(store.ClaimConflict):
            store.release_task("claude", task, done=True, result="different", check=TASK_CHECK, skills=TASK_SKILLS)
        with self.assertRaises(store.ClaimConflict):
            store.take_task("codex", task)

    def test_reply_claim_is_reserved_for_its_author(self):
        question = store.append_message('codex', 'Which version?')['id']
        task = self.user(f'↪ #{question} Use the current version.')['id']
        with self.assertRaisesRegex(store.ClaimConflict, 'addressed to codex.*helper'):
            store.take_task('claude', task)
        store.take_task('codex', task)
        store.join_task('claude', task)
        self.assertEqual(store.list_tasks()[0]['helpers'], ['claude'])

    def test_reply_claim_includes_explicit_mentions_and_allows_admin_replies(self):
        question = store.append_message('codex', 'Ready for review?')['id']
        admin = store.append_message('admin', 'Another question')['id']
        for source, body in ((question, '@claude let us review.'), (question, '@all review.'),
                             (admin, 'Please review.')):
            with self.subTest(source=source, body=body):
                task = self.user(f'↪ #{source} {body}')['id']
                store.take_task('codex', task)
                store.release_task('codex', task, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
                store.take_task('claude', task)
        task = self.user(f'↪ #{admin} @codex please review.')['id']
        with self.assertRaisesRegex(store.ClaimConflict, 'addressed to codex'):
            store.take_task('claude', task)

    def test_named_handoff_reserves_a_reply_for_the_incoming_driver(self):
        question = store.append_message('claude', 'Ready?')['id']
        task = self.user(f'↪ #{question} Proceed.')['id']
        store.take_task('claude', task)
        with self.assertRaises(store.ClaimConflict):
            store.release_task('codex', task, handoff_to='claude', result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        with self.assertRaises(ValueError):
            store.release_task('claude', task, done=True, handoff_to='codex', result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        receipt = store.release_task('claude', task, handoff_to='codex', note='Check the candidate.', result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(store.release_task('claude', task, handoff_to='codex', note='Check the candidate.', result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS), receipt)
        importlib.reload(store)
        self.assertEqual(store.list_tasks()[0]['handoff_to'], 'codex')
        with self.assertRaisesRegex(store.ClaimConflict, 'addressed to codex'):
            store.take_task('claude', task)
        store.take_task('codex', task)
        self.assertEqual(store.list_tasks()[0]['owner'], 'codex')
        self.assertNotIn('handoff_to', store.list_tasks()[0])

    def test_deferred_task_closes_when_its_last_selected_todo_finishes(self):
        task = self.user('Review and test the release.')['id']
        store.take_task('codex', task)
        review = store.add_todo('codex', task, 'AGI', 'Review', 'codex', 'Review the release.')
        proof = store.add_todo('claude', task, 'AGI', 'Test', 'claude', 'Run the candidate.')
        separate = store.add_todo('codex', task, 'AGI', 'Follow-up', 'codex', 'Compare later versions.')
        store.set_next_todo('claude', proof['id'])
        store.join_task('claude', task, todo_id=proof['id'])
        self.assertEqual(store.list_tasks()[0]['state'], 'claimed')
        selected = [proof['id'], review['id']]
        release = store.defer_task('codex', task, selected, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(store.defer_task('codex', task, list(reversed(selected)), result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS), release)
        self.assertEqual(store.active_claims(), {})
        row, = store.list_tasks()
        self.assertEqual((row['state'], row['deferred_todos']), ('released', sorted(selected)))
        store.complete_todo('codex', review['id'], review['revision'], 'Review passed.')
        self.assertEqual(store.list_tasks()[0]['state'], 'released')
        finished = store.complete_todo('claude', proof['id'], proof['revision'], 'Candidate passed.')
        importlib.reload(store)
        row, = store.list_tasks()
        self.assertEqual((row['state'], row['owner'], row['result']), ('done', 'codex', 'Candidate passed.'))
        self.assertEqual(row['event_id'], finished['revision'])
        self.assertEqual(row['helpers'], [])
        self.assertEqual(store.list_todos(), [separate])
        self.assertFalse(next(item for item in store.list_todos(include_done=True) if item['id'] == proof['id'])['is_next'])
        with self.assertRaisesRegex(store.ClaimConflict, 'was completed'):
            store.take_task('claude', task)

    def test_deferred_completion_does_not_close_a_resumed_claim(self):
        task = self.user()['id']
        store.take_task('codex', task)
        item = store.add_todo('codex', task, 'AGI', 'Remaining', 'codex', 'Check the output.')
        store.defer_task('codex', task, [item['id']], result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        store.take_task('codex', task)
        store.complete_todo('codex', item['id'], item['revision'], 'Check passed.')
        self.assertEqual(store.list_tasks()[0]['state'], 'claimed')
        self.assertEqual(store.active_claims(), {f'task:{task}': 'codex'})

    def test_invalid_deferral_keeps_the_current_claim(self):
        task = self.user()['id']
        store.take_task('codex', task)
        item = store.add_todo('codex', task, 'AGI', 'Remaining', 'codex', 'Check the output.')
        with self.assertRaises(store.ClaimConflict):
            store.defer_task('claude', task, [item['id']], result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        for numbers in ([], [item['id'], item['id']], [99999]):
            with self.subTest(numbers=numbers), self.assertRaises(ValueError):
                store.defer_task('codex', task, numbers, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        store.complete_todo('codex', item['id'], item['revision'], 'Check passed.')
        with self.assertRaisesRegex(ValueError, 'No open to-do'):
            store.defer_task('codex', task, [item['id']], result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(store.active_claims(), {f'task:{task}': 'codex'})

    def test_helper_keeps_one_driver_and_can_link_owned_work(self):
        task = self.user("Split the store and screen")['id']
        store.take_task('claude', task)
        before = store.guard_status()
        item = store.add_todo('codex', task, 'AGI', 'Store', 'codex', 'Implement the store.')
        event = store.join_task('codex', task, todo_id=item['id'])
        self.assertEqual(store.join_task('codex', task), event)
        self.assertEqual(store.active_claims(), {f'task:{task}': 'claude'})
        self.assertEqual(store.guard_status()['agent_streak'], before['agent_streak'])
        with self.assertRaises(store.ClaimConflict):
            store.release_task('codex', task, done=True, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        store.complete_todo('codex', item['id'], item['revision'], 'Store tested.')
        importlib.reload(store)
        row, = store.list_tasks()
        self.assertEqual(row['owner'], 'claude')
        self.assertEqual(row['helpers'], ['codex'])
        self.assertEqual(row['helper_todos'], {'codex': item['id']})
        store.leave_task('codex', task)
        self.assertIsNone(store.leave_task('codex', task))
        self.assertEqual(store.list_tasks()[0]['helpers'], [])
        self.assertEqual(store.active_claims(), {f'task:{task}': 'claude'})

    def test_helper_survives_handoff_and_drops_when_promoted_or_closed(self):
        task = self.user()['id']
        with self.assertRaisesRegex(store.ClaimConflict, 'no current driver'):
            store.join_task('codex', task)
        store.take_task('claude', task)
        with self.assertRaisesRegex(store.ClaimConflict, 'already drives'):
            store.join_task('claude', task)
        store.join_task('codex', task)
        store.release_task('claude', task, note='Codex will take over.', result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(store.list_tasks()[0]['helpers'], ['codex'])
        store.take_task('codex', task)
        self.assertEqual(store.list_tasks()[0]['helpers'], [])
        store.join_task('claude', task)
        store.release_task('codex', task, done=True, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(store.list_tasks()[0]['helpers'], [])

    def test_helper_rejects_unrelated_or_other_owner_todo(self):
        task = self.user()['id']
        other_task = self.user('Another request')['id']
        store.take_task('claude', task)
        other_owner = store.add_todo('claude', task, 'AGI', 'Screen', 'claude', 'Build the screen.')
        other_source = store.add_todo('codex', other_task, 'AGI', 'Other', 'codex', 'Try another command.')
        for item in (other_owner, other_source):
            with self.assertRaisesRegex(ValueError, 'open to-do you own'):
                store.join_task('codex', task, todo_id=item['id'])
        self.assertEqual(store.list_tasks()[0]['helpers'], [])

    def test_helper_join_racing_completion_cannot_leave_a_role(self):
        task = self.user()['id']
        store.take_task('claude', task)
        start = threading.Barrier(2)

        def join():
            start.wait(timeout=5)
            try:
                store.join_task('codex', task)
            except store.ClaimConflict:
                pass  # completion can win the lock first

        def complete():
            start.wait(timeout=5)
            store.release_task('claude', task, done=True, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(action) for action in (join, complete)]
            for future in futures:
                future.result(timeout=10)
        row, = store.list_tasks()
        self.assertEqual((row['state'], row['helpers']), ('done', []))

    def test_completion_metadata_is_only_valid_on_agent_task_releases(self):
        completion = dict(self.user(), sender="codex", text="task:1", kind="release", done=True)
        self.assertEqual(store._parse_record(json.dumps(completion), "fixture"), completion)
        for invalid in (dict(sender="user"), dict(kind="msg"), dict(text="README.md"),
                        dict(text="task:0"), dict(done=False), dict(result=" ")):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "Invalid log record"):
                store._parse_record(json.dumps(dict(completion, **invalid)), "fixture")

    def test_handoff_note_round_trips_and_stays_distinct_from_completion(self):
        request = self.user("finish the verification")
        task = request["id"]
        store.take_task("codex", task)
        cli = Path(__file__).resolve().parents[1] / "bin" / "agi"
        result = subprocess.run(
            [str(cli), "release-task", "--as", "codex", str(task), "--result", TASK_RESULT,
             "--check", TASK_CHECK, "--skills", TASK_SKILLS, "--note",
             "Windows rehearsal remains; run the installed candidate next."],
            check=True, capture_output=True, text=True, env=os.environ.copy())
        release = json.loads(result.stdout)
        self.assertEqual(release["note"], "Windows rehearsal remains; run the installed candidate next.")
        row = store.list_tasks()[0]
        self.assertEqual(row["state"], "released")
        self.assertEqual(row["note"], release["note"])
        self.assertEqual((row["result"], row["check"], row["skills"]), (TASK_RESULT, TASK_CHECK, TASK_SKILLS))
        self.assertEqual(store.release_task("codex", task, note=release["note"], result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS), release)

        store.take_task("claude", task)
        completion = store.release_task("claude", task, done=True, result="verified on candidate", check=TASK_CHECK, skills=TASK_SKILLS)
        row = store.list_tasks()[0]
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["result"], "verified on candidate")
        self.assertNotIn("note", row)
        self.assertNotIn("note", completion)

    def test_handoff_note_requires_the_owner_and_valid_release_record(self):
        request = self.user("handoff")
        task = request["id"]
        store.take_task("codex", task)
        with self.assertRaises(store.ClaimConflict):
            store.release_task("claude", task, note="continue", result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        with self.assertRaisesRegex(ValueError, "cannot accompany completion"):
            store.release_task("codex", task, done=True, result="finished", note="continue", check=TASK_CHECK, skills=TASK_SKILLS)
        with self.assertRaisesRegex(ValueError, "at most"):
            store.release_task("codex", task, note="あ" * (store.MAX_HANDOFF_NOTE_CHARS + 1), result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        release = dict(self.user(), sender="codex", text=f"task:{task}", kind="release", note="continue")
        self.assertEqual(store._parse_record(json.dumps(release), "fixture"), release)
        for invalid in (dict(sender="user"), dict(text="README.md"), dict(note=" "), dict(done=True)):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "Invalid log record"):
                store._parse_record(json.dumps(dict(release, **invalid)), "fixture")

    def test_task_releases_allow_handoffs_while_agent_messages_are_paused(self):
        first = self.user("one task")["id"]
        second = self.user("another task")["id"]
        store.take_task("codex", first)
        store.take_task("claude", second)
        store.set_policy("paused")
        self.assertEqual(store.release_task("codex", first, result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)["kind"], "release")
        self.assertEqual(store.release_task("claude", second, note="Try later", result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)["note"], "Try later")
        self.assertTrue(store.guard_status()["blocked"])
        self.assertEqual(store.active_claims(), {})
    def test_task_reminder_reschedule_cancel_and_done_suppression(self):
        request = self.user("verify the next build")
        task_id = request["id"]
        store.take_task("codex", task_id)
        now = time.time()
        first = store.schedule_reminder("user", task_id, now + 20, "check the build")
        moved = store.schedule_reminder("codex", task_id, now + 40, "check the build")
        self.assertEqual(moved["id"], first["id"])
        self.assertEqual(store.next_reminder_deadline(now=now), now + 40)
        self.assertEqual(store.dispatch_due_reminders(now=now + 21), [])
        cancelled = store.cancel_reminder("user", task_id)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(store.next_reminder_deadline(now=now + 41))
        self.assertEqual(store.dispatch_due_reminders(now=now + 41), [])

        second = store.schedule_reminder("user", task_id, now + 50)
        self.assertNotEqual(second["id"], first["id"])
        store.release_task("codex", task_id, done=True, result="verified", check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertIsNone(store.next_reminder_deadline(now=now + 51))
        self.assertEqual(store.dispatch_due_reminders(now=now + 51), [])
        self.assertEqual(store.cancel_reminder("user", task_id, second["id"])["state"], "suppressed")

    def test_due_task_reminder_catches_up_once_after_partial_tail_and_restart(self):
        request = self.user("do the scheduled check")
        task_id = request["id"]
        store.take_task("codex", task_id)
        now = time.time()
        reminder = store.schedule_reminder("user", task_id, now - 10)
        path = next(store.CHAT_DIR.glob("*.jsonl"))
        with path.open("ab") as stream:
            stream.write(b'{"id":999,"sender":"system"')

        write_record = store._write_record
        def fail_after_durable_delivery(*args, **kwargs):
            result = write_record(*args, **kwargs)
            if kwargs.get("task_reminder_ids"):
                raise OSError("simulated interruption after durable journal append")
            return result

        with mock.patch.object(store, "_write_record", side_effect=fail_after_durable_delivery):
            with self.assertRaisesRegex(OSError, "after durable journal append"):
                store.dispatch_due_reminders(now=now)
        self.assertTrue(list((store.DATA_DIR / "recovery").glob("*.partial")))
        delivered = [message for message in store.read_messages()
                     if reminder["id"] in message.get("task_reminder_ids", [])]
        self.assertEqual(len(delivered), 1)

        importlib.reload(store)
        self.assertEqual(store.dispatch_due_reminders(now=now), [])
        retried = store.schedule_reminder("user", task_id, now - 10)
        self.assertEqual(retried["state"], "delivered")
        self.assertEqual(retried["id"], reminder["id"])
        delivered = [message for message in store.read_messages()
                     if reminder["id"] in message.get("task_reminder_ids", [])]
        self.assertEqual(len(delivered), 1)

    def test_concurrent_due_reminder_dispatch_writes_once(self):
        request = self.user("one shared deadline")
        task_id = request["id"]
        store.take_task("codex", task_id)
        now = time.time()
        reminder = store.schedule_reminder("user", task_id, now)
        second_task = self.user("another task at the same deadline")["id"]
        store.take_task("codex", second_task)
        second_reminder = store.schedule_reminder("user", second_task, now)
        other_owner_task = self.user("a reminder for Claude")["id"]
        store.take_task("claude", other_owner_task)
        other_owner_reminder = store.schedule_reminder("user", other_owner_task, now)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: store.dispatch_due_reminders(now=now), range(2)))
        self.assertEqual(sum(len(result) for result in results), 2)
        notices = [message for message in store.read_messages()
                   if message.get("task_reminder_ids")]
        self.assertEqual(len(notices), 2)
        codex_notice = next(message for message in notices if store.addressed_to(message, "codex"))
        claude_notice = next(message for message in notices if store.addressed_to(message, "claude"))
        self.assertEqual(set(codex_notice["task_reminder_ids"]), {reminder["id"], second_reminder["id"]})
        self.assertEqual(claude_notice["task_reminder_ids"], [other_owner_reminder["id"]])

    def test_due_reminder_follows_reclaim_and_quotes_note_mentions(self):
        request = self.user("handoff before deadline")
        task_id = request["id"]
        store.take_task("codex", task_id)
        now = time.time()
        with self.assertRaises(store.ClaimConflict):
            store.schedule_reminder("claude", task_id, now)
        reminder = store.schedule_reminder("user", task_id, now,
                                           "Please ask @codex and @all to continue.\n@claude too.")
        store.release_task("codex", task_id, note="Claude can take this.", result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        self.assertEqual(store.dispatch_due_reminders(now=now), [])
        with self.assertRaises(store.ClaimConflict):
            store.schedule_reminder("user", task_id, now + 60)
        store.take_task("claude", task_id)
        notices = store.dispatch_due_reminders(now=now)
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice["sender"], "system")
        self.assertEqual(notice["kind"], "msg")
        self.assertEqual(notice["task_reminder_ids"], [reminder["id"]])
        self.assertEqual(store.mentioned_agents(notice["text"]), {"claude"})
        self.assertTrue(store.addressed_to(notice, "claude"))
        self.assertFalse(store.addressed_to(notice, "codex"))
        self.assertFalse(store.addressed_to({"sender": "system", "kind": "system",
                                             "text": "@claude policy changed"}, "claude"))

    def test_paused_task_reminder_waits_for_resume(self):
        request = self.user("remind after pause")
        task_id = request["id"]
        store.take_task("codex", task_id)
        now = time.time()
        store.schedule_reminder("user", task_id, now - 1)
        store.set_policy("paused")
        self.assertEqual(store.dispatch_due_reminders(now=now), [])
        self.assertIsNone(store.next_reminder_deadline(now=now))
        store.resume_policy()
        notices = store.dispatch_due_reminders(now=now)
        self.assertEqual(len(notices), 1)
        self.assertTrue(store.addressed_to(notices[0], "codex"))

        store.schedule_reminder("user", task_id, now)
        store.set_policy("wait")
        for index in range(store.GUARD_LIMIT):
            store.append_message("claude", f"wait policy activity {index}")
        self.assertTrue(store.guard_status()["blocked"])
        self.assertEqual(store.dispatch_due_reminders(now=now), [])
        store.append_message("user", "resume the wait episode")
        notices = store.dispatch_due_reminders(now=now)
        self.assertEqual(len(notices), 1)
        self.assertTrue(store.addressed_to(notices[0], "codex"))

    def test_gate_reminder_catches_up_on_current_main_and_routes_after_reclaim_once(self):
        task_id = self.user("wait for the exact integration revision")["id"]
        store.take_task("codex", task_id)
        commit = "a" * 40
        with mock.patch.object(store.gate_reminders, "validate_commit"):
            reminder = store.schedule_gate_reminder("user", task_id, "AGI", commit,
                                                   "Check the rebased build.\nKeep @codex informed.")
        self.assertEqual((reminder["trigger"], reminder["project"], reminder["commit"]), ("gate", "AGI", commit))
        listed = next(row for row in store.list_tasks() if row["id"] == task_id)
        self.assertEqual(listed["reminder_gate"], {"project": "AGI", "commit": commit})
        self.assertNotIn("reminder_due", listed)
        with mock.patch.object(store.gate_reminders, "main_head", return_value="b" * 40) as head, \
             mock.patch.object(store.gate_reminders, "is_ancestor", return_value=False) as ancestor:
            self.assertEqual(store.dispatch_gate_reminders(), [])
            head.assert_called_once_with("AGI")
            ancestor.assert_called_once_with("AGI", commit, "b" * 40)

        store.release_task("codex", task_id, note="Gate finished while this was unclaimed.", result=TASK_RESULT, check=TASK_CHECK, skills=TASK_SKILLS)
        store.take_task("claude", task_id)
        with mock.patch.object(store.gate_reminders, "main_head", return_value="c" * 40) as head, \
             mock.patch.object(store.gate_reminders, "is_ancestor", return_value=True) as ancestor:
            notices = store.dispatch_gate_reminders()
            self.assertEqual(len(notices), 1)
            head.assert_called_once_with("AGI")
            ancestor.assert_called_once_with("AGI", commit, "c" * 40)
        notice = notices[0]
        self.assertEqual(notice["task_reminder_ids"], [reminder["id"]])
        self.assertTrue(store.addressed_to(notice, "claude"))
        self.assertFalse(store.addressed_to(notice, "codex"))
        self.assertIn(commit, notice["text"])
        self.assertIn("> Keep @codex informed.", notice["text"])
        with mock.patch.object(store.gate_reminders, "main_head") as head:
            self.assertEqual(store.dispatch_gate_reminders(), [])
            head.assert_not_called()

    def test_gate_reminders_skip_git_without_eligible_event_or_while_paused(self):
        task_id = self.user("pause a commit wait")["id"]
        store.take_task("codex", task_id)
        with mock.patch.object(store.gate_reminders, "main_head") as head:
            self.assertEqual(store.dispatch_gate_reminders(), [])
            head.assert_not_called()
            with mock.patch.object(store.gate_reminders, "validate_commit"):
                reminder = store.schedule_gate_reminder("user", task_id, "Artlab", "d" * 40)
            row, = store.list_tasks()
            self.assertEqual(row["reminder_gate"]["project"], "Artlab")
            store.set_policy("paused")
            self.assertEqual(store.dispatch_gate_reminders(), [])
            head.assert_not_called()
            store.resume_policy()
            with mock.patch.object(store.gate_reminders, "is_ancestor", return_value=True):
                delivered, = store.dispatch_gate_reminders()
                self.assertIn("Artlab main", delivered["text"])
        self.assertEqual(reminder["project"], "Artlab")

    def test_gate_reminder_cancel_done_and_timed_reminder_conflicts(self):
        task_id = self.user("cancel the gate wait")["id"]
        store.take_task("codex", task_id)
        due_at = time.time() + 60
        timed = store.schedule_reminder("user", task_id, due_at)
        with mock.patch.object(store.gate_reminders, "validate_commit"):
            with self.assertRaises(store.ClaimConflict):
                store.schedule_gate_reminder("user", task_id, "AGI", "e" * 40)
        store.cancel_reminder("user", task_id, timed["id"])
        with mock.patch.object(store.gate_reminders, "validate_commit"):
            gate = store.schedule_gate_reminder("user", task_id, "AGI", "e" * 40)
            with self.assertRaises(store.ClaimConflict):
                store.schedule_reminder("user", task_id, due_at + 60)
        cancelled = store.cancel_reminder("user", task_id, gate["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        with mock.patch.object(store.gate_reminders, "main_head") as head:
            self.assertEqual(store.dispatch_gate_reminders(), [])
            head.assert_not_called()

        other_task = self.user("complete before gate delivery")["id"]
        store.take_task("codex", other_task)
        with mock.patch.object(store.gate_reminders, "validate_commit"):
            store.schedule_gate_reminder("user", other_task, "ArtLab", "f" * 40)
        store.release_task("codex", other_task, done=True, result="finished", check=TASK_CHECK, skills=TASK_SKILLS)
        with mock.patch.object(store.gate_reminders, "main_head") as head:
            self.assertEqual(store.dispatch_gate_reminders(), [])
            head.assert_not_called()

    def test_gate_dispatch_rechecks_cancellation_after_git_check(self):
        task_id = self.user("cancel during the main-ref check")["id"]
        store.take_task("codex", task_id)
        with mock.patch.object(store.gate_reminders, "validate_commit"):
            reminder = store.schedule_gate_reminder("user", task_id, "AGI", "1" * 40)

        def reaches_main(project, commit, head):
            store.cancel_reminder("user", task_id, reminder["id"])
            return True

        with mock.patch.object(store.gate_reminders, "main_head", return_value="2" * 40), \
             mock.patch.object(store.gate_reminders, "is_ancestor", side_effect=reaches_main):
            self.assertEqual(store.dispatch_gate_reminders(), [])
        self.assertFalse(any(message.get("task_reminder_ids") for message in store.read_messages()))

    def test_reminder_cli_schedules_and_cancels_by_stable_id_or_task(self):
        request = self.user("check the candidate")
        task_id = request["id"]
        store.take_task("codex", task_id)
        cli = Path(__file__).resolve().parents[1] / "bin" / "agi"
        stable_id = "a" * 32
        before = time.time()
        scheduled = subprocess.run(
            [str(cli), "remind", "--as", "codex", str(task_id), "--in", "3600",
             "--note", "Check the installed build.", "--request-id", stable_id],
            check=True, capture_output=True, text=True, env=os.environ.copy())
        value = json.loads(scheduled.stdout)
        self.assertEqual(value["id"], stable_id)
        self.assertEqual(value["state"], "scheduled")
        self.assertEqual(value["note"], "Check the installed build.")
        self.assertGreaterEqual(value["due_at"], before + 3600)
        replay = subprocess.run(scheduled.args, check=True, capture_output=True, text=True, env=os.environ.copy())
        self.assertEqual(json.loads(replay.stdout), value)

        cancelled = subprocess.run(
            [str(cli), "cancel-reminder", "--as", "codex", str(task_id), "--request-id", stable_id],
            check=True, capture_output=True, text=True, env=os.environ.copy())
        self.assertEqual(json.loads(cancelled.stdout)["state"], "cancelled")

        another = store.schedule_reminder("user", task_id, time.time() + 7200)
        cancelled_by_task = subprocess.run(
            [str(cli), "cancel-reminder", "--as", "codex", str(task_id)],
            check=True, capture_output=True, text=True, env=os.environ.copy())
        self.assertEqual(json.loads(cancelled_by_task.stdout)["id"], another["id"])
        self.assertEqual(json.loads(cancelled_by_task.stdout)["state"], "cancelled")

        invalid = subprocess.run(
            [str(cli), "remind", "--as", "codex", str(task_id), "--in", "-1"],
            capture_output=True, text=True, env=os.environ.copy())
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("finite non-negative", invalid.stderr)

    def test_claim_overlap_release_and_escape(self):
        store.append_message('codex','agi','claim')
        with self.assertRaises(store.ClaimConflict):store.append_message('claude','agi/ui.py','claim')
        with self.assertRaises(store.ClaimConflict):store.append_message('claude','agi','release')
        with self.assertRaises(ValueError):store.append_message('codex','../outside','claim')
        store.append_message('codex','agi','release')
        self.assertEqual(store.active_claims(),{})

    def test_normalized_file_claim_cannot_use_task_namespace(self):
        addressed = self.user("@claude the request is yours")["id"]
        for alias in (f"./task:{addressed}", str(store.CHECKOUT_ROOT / f"task:{addressed}"), "./task:note"):
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, "reserved task: namespace"):
                store.append_message("codex", alias, "claim")
        with self.assertRaises(store.ClaimConflict):
            store.take_task("codex", addressed)
        self.assertEqual(store.take_task("claude", addressed)["text"], f"task:{addressed}")
        # Older accepted file aliases are still readable and releasable by their owner.
        with store._locked(exclusive=True):
            store._write_record("codex", "task:note", "claim", store._records())
        store.append_message("codex", "./task:note", "release")
        self.assertNotIn("task:note", store.active_claims())

    def test_wait_arrival_and_timeout(self):
        self.assertFalse(store.wait_for_change(0,0.01))
        thread=threading.Thread(target=lambda:(time.sleep(.05),self.user()))
        thread.start()
        try:self.assertTrue(store.wait_for_change(0,2))
        finally:thread.join()
        self.assertTrue(store.wait_for_change(0,0))

    def test_poll_fallback(self):
        with mock.patch.object(store,'_inotify',return_value=None):
            thread=threading.Thread(target=lambda:(time.sleep(.05),self.user()))
            thread.start()
            try:self.assertTrue(store.wait_for_change(0,2))
            finally:thread.join()

    def test_presence_expiry_and_continuity(self):
        with mock.patch.object(store.time,'time',return_value=100):
            self.assertEqual(store.get_presence()['codex']['state'],'unknown')
            first=store.set_presence('codex','working',ttl=10)
        with mock.patch.object(store.time,'time',return_value=105):
            second=store.set_presence('codex','working',ttl=10)
            self.assertEqual(first['since'],second['since'])
        with mock.patch.object(store.time,'time',return_value=116):
            self.assertEqual(store.get_presence()['codex']['state'],'unknown')
            third=store.set_presence('codex','working',ttl=10)
            self.assertEqual(third['since'],116)
        self.assertEqual(store.last_id(),0)

    def test_presence_does_not_take_log_lock(self):
        with mock.patch.object(store,'_locked',side_effect=AssertionError('log lock used')):
            store.set_reachable('claude')
            self.assertTrue(store.get_presence()['claude']['reachable'])
            store.set_presence('codex','idle')

    def test_mailbox_heartbeats_do_not_change_an_active_turn(self):
        with mock.patch.object(store.time, 'time', return_value=100):
            active = store.set_presence('claude', 'working', ttl=10)
        with mock.patch.object(store.time, 'time', return_value=107):
            store.set_reachable('claude', ttl=20)
            self.assertEqual(store.get_presence()['claude'], active)
        with mock.patch.object(store.time, 'time', return_value=111):
            expired = store.get_presence()['claude']
            self.assertEqual(expired['state'], 'unknown')
            self.assertTrue(expired['reachable'])
            self.assertIsNone(expired['since'])
            store.set_presence('claude', 'idle', ttl=20)
            self.assertEqual(store.get_presence()['claude']['state'], 'idle')
            store.set_presence('claude', 'working', ttl=20)
            store.clear_presence('claude')
            self.assertFalse(store.get_presence()['claude']['reachable'])

    def test_legacy_delivery_state_establishes_contact_only(self):
        with mock.patch.object(store.time, 'time', return_value=101):
            path = store._presence_directory() / 'codex.json'
            path.write_text(json.dumps(dict(state='working', updated_at=100, since=100, expires_at=150, note='')))
            presence = store.get_presence()['codex']
            self.assertTrue(presence['reachable'])
            self.assertEqual(presence['state'], 'idle')
            self.assertIsNone(presence['since'])

    def test_a_stopped_turn_cannot_keep_a_dead_mailbox_reachable(self):
        with mock.patch.object(store.time, 'time', return_value=100):
            store.set_reachable('claude', ttl=30)
            store.set_presence('claude', 'working', ttl=300)
        with mock.patch.object(store.time, 'time', return_value=109):
            idle = store.set_presence('claude', 'idle', ttl=900)
            self.assertTrue(idle['reachable'])
            self.assertEqual(store.get_presence()['claude'], idle)
        with mock.patch.object(store.time, 'time', return_value=131):
            presence = store.get_presence()['claude']
            self.assertEqual(presence['state'], 'unknown')
            self.assertFalse(presence['reachable'])

    def test_presence_malformed_and_validation(self):
        store.set_presence('codex','idle')
        path=store.DATA_DIR/'presence/codex.json'
        for value in ['{', 'null', '{"state":"working"}', '[]']:
            path.write_text(value)
            self.assertEqual(store.get_presence()['codex']['state'],'unknown')
        for ttl in [0,-1,float('nan'),float('inf'),True]:
            with self.assertRaises(ValueError):store.set_presence('codex','working',ttl=ttl)
        with self.assertRaises(ValueError):store.set_presence('codex','working','x'*201)
        with self.assertRaises(ValueError):store.set_presence('codex','listening')

    def test_repeated_unacked_wait_preserves_working_since(self):
        active = store.set_presence('codex', 'working', 'native turn')
        self.user()
        launcher = Path(store.__file__).resolve().parents[1] / 'bin/agi'
        def wait():
            subprocess.run([str(launcher), 'wait', '--as', 'codex', '--timeout', '0.05'],
                           capture_output=True, text=True, check=True, timeout=10)
            return store.get_presence()['codex']
        first = wait()
        time.sleep(0.02)
        second = wait()
        self.assertEqual(first, active)
        self.assertEqual(second, active)
        self.assertEqual(store.get_ack('codex'), 0)

    def test_compact_wait_preserves_every_record_text_order_and_ack(self):
        records = [store.append_message('user', 'hello\n"quotes" · violet', request_id='a'*32),
                   store.append_message('codex', 'README.md', 'claim'),
                   store.append_message('codex', 'README.md', 'release')]
        launcher = Path(store.__file__).resolve().parents[1] / 'bin/agi'
        command = [str(launcher), 'wait', '--as', 'codex', '--timeout', '0']
        full = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        compact = subprocess.run(command + ['--compact'], capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in full.stdout.splitlines()], records)
        expected = [[row[key] for key in ('id', 'sender', 'kind', 'text')] for row in records]
        self.assertEqual([json.loads(line) for line in compact.stdout.splitlines()], expected)
        self.assertLess(len(compact.stdout.encode()), len(full.stdout.encode()))
        self.assertEqual(store.get_ack('codex'), 0)
        store.set_ack('codex', records[-1]['id'])
        empty = subprocess.run(command + ['--compact'], capture_output=True, text=True, timeout=10)
        self.assertEqual((empty.returncode, empty.stdout), (2, ''))

    def test_nonblocking_cli_wait_does_not_overwrite_watcher_presence(self):
        launcher = Path(store.__file__).resolve().parents[1] / 'bin/agi'
        lease = store.set_presence('codex', 'working', 'active turn')
        command = [str(launcher), 'wait', '--as', 'codex', '--timeout', '0', '--compact']
        empty = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual((empty.returncode, empty.stdout), (2, ''))
        self.assertEqual(store.get_presence()['codex'], lease)
        self.user()
        unread = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(unread.stdout)[:3], [1, 'user', 'msg'])
        self.assertEqual(store.get_presence()['codex'], lease)
        self.assertEqual(store.get_ack('codex'), 0)

    def test_cli_version_matches_release_file_without_creating_runtime_data(self):
        root=Path(store.__file__).resolve().parents[1]
        target=Path(self.directory.name)/'unused-version-data'
        result=subprocess.run([str(root/'bin/agi'),'--version'],cwd=self.directory.name,
                    env=dict(os.environ,AGI_DATA=str(target)),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        expected=(root/'VERSION').read_text(encoding='ascii').strip()
        self.assertEqual(result.stdout,f'AGI {expected}\n')
        self.assertFalse(target.exists())

    def test_cli_wait_lease_and_exit_codes(self):
        launcher=Path(store.__file__).resolve().parents[1]/'bin/agi'
        result=subprocess.run([str(launcher),'wait','--as','codex','--timeout','0.05'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(store.get_presence()['codex']['state'],'idle')
        self.assertTrue(store.get_presence()['codex']['reachable'])
        self.user()
        result=subprocess.run([str(launcher),'wait','--as','codex','--timeout','0.05'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(store.get_presence()['codex']['state'],'idle')
        self.assertEqual(store.get_ack('codex'),0)

    def test_custom_rate_window_and_user_does_not_reset(self):
        class PolicyClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(store.time.time(), tz)

        with (mock.patch.object(store.time, 'time', return_value=1_800_000_000) as clock,
              mock.patch.object(store, 'datetime', PolicyClock)):
            store.set_policy("custom", limit=store.RATE_LIMIT, window_s=store.RATE_WINDOW, duration_s=3600)
            for index in range(store.RATE_LIMIT):
                store.append_message(store.AGENTS[index % 2], str(index))
            status = store.guard_status()
            self.assertEqual(status['mode'], 'custom')
            self.assertTrue(status['blocked'])
            self.assertEqual(status['retry_after'], store.RATE_WINDOW)
            self.user('user still allowed')
            for _ in range(2):
                with self.assertRaisesRegex(store.GuardBlocked, 'retry after'):
                    store.append_message('codex', 'blocked')
            notices = [m for m in store.read_messages() if m['text'].startswith(store._RATE_NOTICE)]
            self.assertEqual(len(notices), 1)

            clock.return_value += store.RATE_WINDOW - 1
            status = store.guard_status()
            self.assertTrue(status['blocked'])
            self.assertEqual(status['retry_after'], 1)
            clock.return_value += 1
            self.assertFalse(store.guard_status()['blocked'])
            store.append_message('codex', 'resumed without user')

    def test_custom_expiry_disable_and_malformed(self):
        start=time.time()
        store.set_policy("custom", limit=store.RATE_LIMIT, window_s=store.RATE_WINDOW, duration_s=30)
        for i in range(10):store.append_message('codex',str(i))
        with mock.patch.object(store.time,'time',return_value=start+31):
            self.assertEqual(store.guard_status()['mode'],'normal')
            self.assertTrue(store.guard_status()['blocked'])
        store.set_policy("custom", limit=store.RATE_LIMIT, window_s=store.RATE_WINDOW, duration_s=100)
        store.set_policy("normal")
        self.assertEqual(store.guard_status()['mode'],'normal')
        for text in ['null','[]','{','{"expires_at":"forever"}']:
            (store.DATA_DIR/'policy.json').write_text(text)
            self.assertEqual(store.guard_status()['mode'],'wait')
        for value in [0,-1,86401,True,float('inf'),float('nan')]:
            with self.assertRaises(ValueError):store.set_policy("custom", limit=store.RATE_LIMIT, window_s=store.RATE_WINDOW, duration_s=value)

    def test_watch_rearms_after_chat_directory_replacement(self):
        installed=threading.Event()
        original=store._inotify
        def watch():
            fd=original()
            installed.set()
            return fd
        def producer():
            installed.wait(2)
            store.CHAT_DIR.rename(store.DATA_DIR/'old-chat')
            store.CHAT_DIR.mkdir(exist_ok=True)
            time.sleep(.15)
            self.user('new directory')
        thread=threading.Thread(target=producer)
        thread.start()
        started=time.monotonic()
        try:
            with mock.patch.object(store,'_inotify',side_effect=watch):
                self.assertTrue(store.wait_for_change(0,3))
        finally:thread.join()
        self.assertLess(time.monotonic()-started,2,'watch stayed on the renamed directory')

    def test_malformed_ack_is_an_actionable_error(self):
        store.get_ack('codex')
        for text in ['null','[]','{','{}','{"acked":true}','{"acked":-1}']:
            (store.CURSOR_DIR/'codex.json').write_text(text)
            with self.assertRaisesRegex(ValueError,'Invalid acknowledgment file for codex'):
                store.get_ack('codex')

    def test_wake_timing_broadcast_range_and_ack_coverage(self):
        first = self.user("first")
        last = self.user("last")
        posted = store._timestamp(last, None)
        store.woke("codex", last["id"], first_id=first["id"], queued_at=posted + 3)
        store.woke("codex", last["id"], queued_at=posted + 9)
        self.assertEqual(store.get_ack("codex"), 0)
        self.assertEqual(store.wake_ranges("codex"), ((first["id"], last["id"]),))
        self.assertEqual(store.wake_ranges("claude"), ())
        with mock.patch.object(store.time, "time", return_value=posted + 12):
            waiting = store.wake_timing("codex")["oldest_unacked"]
            self.assertEqual(waiting["id"], first["id"])
            self.assertTrue(waiting["queued"])
            store.set_ack("codex", first["id"])
            self.assertEqual(store.wake_timing("codex")["last"],
                             {"id": last["id"], "wake_s": 3.0, "ack_s": None})
            store.set_ack("codex", last["id"])
        result = store.wake_timing("codex")
        self.assertEqual(result["recent"], [{"id": last["id"], "wake_s": 3.0, "ack_s": 12.0}])
        self.assertIsNone(result["oldest_unacked"])
        self.assertEqual(store.wake_timing("claude")["recent"], [])

    def test_wake_receipts_are_bounded_and_allow_late_queue_receipts(self):
        self.fixture_log('2026-11-16.jsonl', range(1, 22))
        posted = store._timestamp(store.read_messages()[0], None)
        fixture = [dict(id=id_, first_id=id_, queued_at=posted+1, posted_at=posted, acked_at=None)
                   for id_ in range(1, 21)]
        cursor = store.CURSOR_DIR / "codex.json"
        cursor.write_text(json.dumps({"acked": 0, "wakes": fixture}))
        with mock.patch.object(store.time, "time", return_value=posted + 4):
            store.set_ack("codex", 21)
            store.woke("codex", 21, queued_at=posted + 2)
        timing = store.wake_timing("codex")
        self.assertEqual([row["id"] for row in timing["recent"]], list(range(2, 22)))
        self.assertEqual(timing["last"], {"id": 21, "wake_s": 2.0, "ack_s": 4.0})
        self.assertEqual(store.wake_ranges("codex"), tuple((id_, id_) for id_ in range(2, 22)))

    def test_human_task_claim_has_clear_error(self):
        task=self.user()['id']
        with self.assertRaisesRegex(ValueError,'Task ownership is for agents'):
            store.append_message('user',f'task:{task}','claim')

    def test_derived_state_only_parses_new_agent_timestamps(self):
        self.user()
        store.append_message('codex','one')
        store.guard_status()
        with mock.patch.object(store,'_timestamp',wraps=store._timestamp) as stamp:
            store.guard_status()
            self.assertEqual(stamp.call_count,0)
            store.append_message('claude','two')
            self.assertEqual(store.guard_status()['agent_streak'],2)
            store.active_claims()
            self.assertEqual(stamp.call_count,1)

    def test_derived_claims_rebuild_on_replacement(self):
        store.append_message('codex','README.md','claim')
        claims=store.active_claims()
        claims.clear()
        self.assertEqual(store.active_claims(),{'README.md':'codex'})
        path=next(store.CHAT_DIR.glob('*.jsonl'))
        replacement=path.with_suffix('.new')
        replacement.write_text('')
        replacement.replace(path)
        self.assertEqual(store.active_claims(),{})
        self.assertEqual(store.guard_status()['agent_streak'],0)

    def test_committed_log_survives_sequence_metadata_failure(self):
        original=store._atomic_write
        def fail_sequence(path,data):
            if path.name=='seq':raise OSError('simulated interruption after log commit')
            original(path,data)
        with mock.patch.object(store,'_atomic_write',side_effect=fail_sequence):
            committed=self.user('committed despite metadata failure')
        self.assertEqual(committed['id'],1)
        self.assertEqual(store.read_messages(),[committed])
        self.assertEqual(store.last_id(),1)
        self.assertEqual(self.user('next')['id'],2)

    def test_log_durability_failure_still_propagates(self):
        original=store._sync_directory
        def fail_log_directory(path):
            if path==store.CHAT_DIR:raise OSError('simulated log-directory sync failure')
            original(path)
        with mock.patch.object(store,'_sync_directory',side_effect=fail_log_directory):
            with self.assertRaisesRegex(OSError,'log-directory sync failure'):
                self.user('durability not confirmed')

    def test_failed_recovery_save_never_truncates_original_tail(self):
        self.user()
        path=next(store.CHAT_DIR.glob('*.jsonl'))
        with path.open('ab') as stream:stream.write(b'partial suffix')
        original=path.read_bytes()
        with mock.patch.object(store,'_atomic_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.user('cannot recover yet')
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(self.user('retry after space restored')['id'],2)

    def test_dead_writer_releases_advisory_lock(self):
        store.get_ack('codex')
        code='from agi import store; import time; ctx=store._locked(exclusive=True); ctx.__enter__(); print("locked",flush=True); time.sleep(20)'
        env=dict(os.environ,PYTHONPATH=str(Path(store.__file__).resolve().parents[1]))
        process=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            process.kill()
            process.wait(timeout=3)
            self.assertEqual(self.user('after dead writer')['id'],1)
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)
            process.stdout.close()

    def test_history_inspection_is_bounded_and_does_not_ack(self):
        for number in range(5):self.user('message '+str(number))
        recent=store.read_tail(2)
        self.assertEqual([m['id'] for m in recent],[4,5])
        recent[0]['text']='external mutation'
        self.assertEqual(store.read_tail(2)[0]['text'],'message 3')
        self.assertEqual(store.read_tail(0),[])
        launcher=Path(store.__file__).resolve().parents[1]/'bin/agi'
        for args,expected in [(['log','--last','2'],[4,5]),(['show','2'],[2])]:
            result=subprocess.run([str(launcher),*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual([json.loads(line)['id'] for line in result.stdout.splitlines()],expected)
            self.assertEqual(store.get_ack('codex'),0)
            self.assertEqual(store.get_presence()['codex']['state'],'unknown')
        for args in [['show','999'],['show','0'],['log','--last','1001'],['log','--last','-1']]:
            result=subprocess.run([str(launcher),*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertTrue(result.stderr.startswith('chat: '))

    def test_prepare_retries_directory_replacement_race(self):
        original=Path.mkdir
        raced=[]
        def mkdir(path,*args,**kwargs):
            if path==store.CHAT_DIR and not raced:
                raced.append(True)
                raise FileExistsError('simulated mkdir/is_dir replacement race')
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'mkdir',new=mkdir):store._prepare()
        self.assertTrue(store.CHAT_DIR.is_dir())
        self.assertTrue(raced)

    def test_prepare_refuses_real_file_collision(self):
        store.CHAT_DIR.write_text('not a directory')
        with self.assertRaises(FileExistsError):store._prepare()
        self.assertEqual(store.CHAT_DIR.read_text(),'not a directory')

    def fixture_log(self,name,ids):
        store._prepare()
        path=store.CHAT_DIR/name
        with path.open('w') as stream:
            for number in ids:
                stream.write(json.dumps({'id':number,'ts':'2026-11-16T10:00:00+01:00',
                                         'sender':'user','kind':'msg','text':'fixture '+str(number)})+'\n')
        return path

    def test_tail_decodes_only_bounded_rows(self):
        self.fixture_log('2026-11-16.jsonl',range(1,1001))
        with mock.patch.object(store,'_parse_record',wraps=store._parse_record) as parse:
            rows=store.read_tail(10)
            self.assertEqual([m['id'] for m in rows],list(range(991,1001)))
            self.assertEqual(parse.call_count,11)
        self.assertEqual(store._record_cache,[])
        rows[0]['text']='changed'
        self.assertNotEqual(store.read_tail(10)[0]['text'],'changed')

    def test_tail_merges_clock_rollback_files_by_id(self):
        self.fixture_log('2026-11-18.jsonl',[1,4,7])
        self.fixture_log('2026-11-16.jsonl',[2,5,8])
        self.fixture_log('2026-11-17.jsonl',[3,6,9])
        self.assertEqual([m['id'] for m in store.read_tail(4)],[6,7,8,9])
        self.assertEqual(len(store.read_tail(20)),9)

    def test_tail_ignores_partial_suffix_and_empty_files(self):
        path=self.fixture_log('2026-11-16.jsonl',[1,2])
        with path.open('ab') as stream:stream.write(b'{"id":3')
        (store.CHAT_DIR/'2026-11-17.jsonl').write_bytes(b'')
        self.assertEqual([m['id'] for m in store.read_tail(2)],[1,2])
        self.assertEqual(store.read_tail(0),[])

    def test_tail_rejects_selected_corruption(self):
        path=self.fixture_log('2026-11-16.jsonl',[1,2])
        with path.open('ab') as stream:stream.write(b'not json\n')
        with self.assertRaisesRegex(ValueError,'Invalid log record'):store.read_tail(2)
        self.fixture_log('2026-11-16.jsonl',[2,1])
        with self.assertRaisesRegex(ValueError,'must increase'):store.read_tail(2)

    def test_search_kind_filters_before_limit_across_large_control_backlog(self):
        store._prepare()
        records = [dict(id=i, ts='2026-11-17T00:00:00+00:00', sender='user' if i<3 else 'system',
                        kind='msg' if i<3 else 'system', text='Needle') for i in range(1,1005)]
        (store.CHAT_DIR/'2026-11-17.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in records))
        self.assertEqual(store.search_messages('needle',limit=1)[0]['id'],1004)
        found = store.search_messages('needle',limit=1,kind='msg')
        self.assertEqual(found[0]['id'],2)
        self.assertEqual(store.search_messages('needle',limit=1,before_id=2,kind='msg')[0]['id'],1)
        self.assertEqual([m['id'] for m in store.search_messages('needle',kind='msg')],[1,2])
        self.assertEqual(store.search_messages('needle',kind='claim'),[])
        found[0]['text']='changed by caller'
        self.assertEqual(store.search_messages('needle',limit=1,kind='msg')[0]['text'],'Needle')
        self.assertEqual(store.get_ack('codex'),0)
        for kind in ('bogus',False,[],1):
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'search kind'):
                store.search_messages('needle',limit=0,kind=kind)

    def test_search_literal_casefold_pagination_without_ack(self):
        self.user('Straße [literal] one');self.user('other');self.user('STRASSE [literal] two')
        self.assertEqual([m['id'] for m in store.search_messages('strasse')],[1,3])
        self.assertEqual([m['id'] for m in store.search_messages('[literal]',limit=1)],[3])
        self.assertEqual([m['id'] for m in store.search_messages('strasse',before_id=3)],[1])
        self.assertEqual(store.search_messages('strasse',limit=0),[])
        self.assertEqual(store.get_ack('codex'),0)
        with self.assertRaises(ValueError):store.search_messages('   ')
        launcher=Path(store.__file__).resolve().parents[1]/'bin/agi'
        result=subprocess.run([str(launcher),'search','strasse','--last','1','--before','3'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['id'],1)
        self.assertEqual(store.get_ack('codex'),0)


    def test_cold_recent_cursor_uses_tail_without_full_scan(self):
        self.fixture_log('2026-11-16.jsonl',range(1,2001))
        with mock.patch.object(store,'_records',side_effect=AssertionError('unexpected full scan')):
            self.assertEqual([m['id'] for m in store.read_messages(1998)],[1999,2000])
            self.assertEqual(store.read_messages(2000),[])

    def test_cold_old_cursor_does_not_lose_unread_backlog(self):
        self.fixture_log('2026-11-16.jsonl',range(1,2001))
        rows=store.read_messages(1)
        self.assertEqual(len(rows),1999)
        self.assertEqual(rows[0]['id'],2)
        self.assertEqual(rows[-1]['id'],2000)

    def test_request_retry_returns_original_without_second_post(self):
        token='a'*32
        first=store.append_message('user','one delivery',request_id=token)
        again=store.append_message('user','one delivery',request_id=token)
        self.assertEqual(first,again)
        self.assertEqual(len(store.read_messages()),1)
        self.assertEqual(store.find_request(token),first)
        with self.assertRaisesRegex(ValueError,'different message'):
            store.append_message('user','changed',request_id=token)
        legacy=store.append_message('user','legacy call')
        self.assertNotIn('request_id',legacy)

    def test_concurrent_request_retries_have_one_id(self):
        token='b'*32
        code=f'from agi import store; print(store.append_message("user","concurrent intent",request_id="{token}")["id"])'
        env=dict(os.environ,PYTHONPATH=str(Path(store.__file__).resolve().parents[1]))
        processes=[subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,text=True) for _ in range(2)]
        ids=[]
        for process in processes:
            output,_=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0);ids.append(int(output))
        self.assertEqual(ids,[1,1])
        self.assertEqual(len(store.read_messages()),1)

    def test_duplicate_request_ids_in_log_are_rejected(self):
        message=store.append_message('user','original',request_id='c'*32)
        message['id']=2
        path=next(store.CHAT_DIR.glob('*.jsonl'))
        with path.open('a') as stream:stream.write(json.dumps(message)+'\n')
        with self.assertRaisesRegex(ValueError,'duplicate request id'):store.read_messages()

    def test_request_retry_must_confirm_durability(self):
        token='d'*32
        store.append_message('user','one durable intent',request_id=token)
        with mock.patch.object(store,'_sync_committed_logs',side_effect=OSError('sync unavailable')):
            with self.assertRaises(OSError):
                store.append_message('user','one durable intent',request_id=token)
        self.assertEqual(len(store.read_messages()),1)



class PolicyTests(StoreFixture):
    def test_default_normal_refills_without_user_reset(self):
        status = store.guard_status()
        self.assertEqual((status['mode'], status['limit']), ('normal', 5))
        for i in range(5):store.append_message(store.AGENTS[i%2], str(i))
        self.user('does not reset the rolling window')
        store.append_message('codex', 'README.md', 'claim')
        status=store.guard_status()
        self.assertTrue(status['blocked']);self.assertEqual(status['used'], 5)
        self.assertEqual(status['window_seconds'], 60)
        with self.assertRaises(store.GuardBlocked):store.append_message('claude', 'sixth')
        with mock.patch.object(store.time, 'time', return_value=time.time()+61):
            self.assertFalse(store.guard_status()['blocked'])

    def test_custom_limit_window_duration_and_expiry(self):
        start=time.time();store.set_policy('custom', limit=10, window_s=60, duration_s=3600)
        for i in range(10):store.append_message(store.AGENTS[i%2], str(i))
        status=store.guard_status();self.assertEqual(status['used'],10)
        self.assertTrue(status['blocked']);self.assertGreater(status['retry_after'],0)
        self.user('still does not reset')
        self.assertTrue(store.guard_status()['blocked'])
        with mock.patch.object(store.time,'time',return_value=start+61):
            self.assertFalse(store.guard_status()['blocked'])
        with mock.patch.object(store.time,'time',return_value=start+3601):
            self.assertEqual(store.guard_status()['mode'],'normal')

    def test_expiry_keeps_recent_message_usage(self):
        start=time.time();store.set_policy('custom',limit=10,window_s=60,duration_s=5)
        for i in range(6):store.append_message('codex',str(i))
        with mock.patch.object(store.time,'time',return_value=start+6):
            status=store.guard_status();self.assertEqual(status['mode'],'normal')
            self.assertTrue(status['blocked']);self.assertEqual(status['used'],6)

    def test_persistent_custom_refills_and_survives_pause(self):
        start = time.time()
        store.set_policy('custom', limit=1, window_s=60)
        store.append_message('codex', 'uses the budget')
        with self.assertRaises(store.GuardBlocked):
            store.append_message('claude', 'no capacity')

        with mock.patch.object(store.time, 'time', return_value=start + 86401):
            status = store.guard_status()
            self.assertEqual((status['mode'], status['limit'], status['expires_at']), ('custom', 1, None))
            self.assertFalse(status['blocked'])
            self.assertEqual(store.set_policy('paused')['mode'], 'paused')
            resumed = store.resume_policy()
            self.assertEqual((resumed['mode'], resumed['limit'], resumed['expires_at']), ('custom', 1, None))

    def test_persistent_custom_requires_an_explicit_null_expiry_on_disk(self):
        store.set_policy('custom', limit=4, window_s=60)
        path = store.DATA_DIR / 'policy.json'
        saved = json.loads(path.read_text())
        self.assertIsNone(saved.pop('expires_at'))
        path.write_text(json.dumps(saved))
        self.assertEqual(store.guard_status()['mode'], 'wait')

    def test_legacy_normal_policy_adopts_the_new_limit_even_after_pause(self):
        path = store.DATA_DIR / 'policy.json'
        store.set_policy('normal')
        normal = json.loads(path.read_text())
        for old_limit in (4, 10):
            with self.subTest(old_limit=old_limit):
                legacy = dict(normal, limit=old_limit)
                path.write_text(json.dumps(legacy))
                status = store.guard_status()
                self.assertEqual((status['mode'], status['limit']), ('normal', 5))

                store.set_policy('paused')
                path.write_text(json.dumps(dict(json.loads(path.read_text()), resume=legacy)))
                self.assertEqual(store.guard_status()['mode'], 'paused')
                status = store.resume_policy()
                self.assertEqual((status['mode'], status['limit']), ('normal', 5))
                self.assertEqual(json.loads(path.read_text())['limit'], 5)

    def test_wait_starts_fresh_and_user_resets_it(self):
        for i in range(4):store.append_message('codex',str(i))
        store.set_policy('wait')
        for i in range(4):store.append_message('codex',str(i))
        self.assertTrue(store.guard_status()['blocked'])
        self.user();self.assertFalse(store.guard_status()['blocked'])
        store.append_message('claude','after user')

    def test_pause_does_not_resume_on_user_and_keeps_target(self):
        store.set_policy('custom',limit=10,window_s=60,duration_s=3600)
        first=store.set_policy('paused');again=store.set_policy('paused')
        self.assertEqual(first,again)
        for _ in range(2):
            self.user()
            with self.assertRaisesRegex(store.GuardBlocked,'paused'):store.append_message('codex','no')
        notices=[m for m in store.read_messages() if m['text'].startswith(store._PAUSE_NOTICE)]
        self.assertEqual(len(notices),1)
        status=store.resume_policy();self.assertEqual(status['mode'],'custom')
        self.assertEqual(status['limit'],10)
        store.append_message('codex','resumed')

        store.set_policy('paused')
        for _ in range(2):
            with self.assertRaisesRegex(store.GuardBlocked, 'paused'):
                store.append_message('codex', 'still paused')
        notices = [m for m in store.read_messages() if m['text'].startswith(store._PAUSE_NOTICE)]
        self.assertEqual(len(notices), 2)

    def test_paused_notice_scan_stops_at_pause_watermark(self):
        visited = []

        class History(list):
            def __reversed__(self):
                for message in super().__reversed__():
                    visited.append(message['id'])
                    yield message

        watermark = 1000
        messages = History(dict(id=number, sender='user', kind='msg', text='hello')
                           for number in range(1, watermark + 3))
        messages[watermark - 1] = dict(id=watermark, sender='system', kind='system',
                                       text=store._PAUSE_NOTICE + 'previous episode')
        store._summary(messages)  # Isolate notice lookup from the initial summary build.
        policy = store._policy('paused', start_after_id=watermark)
        with mock.patch.object(store, '_current_policy', return_value=policy):
            status, noticed = store._guard_state(messages)
        self.assertTrue(status['blocked'])
        self.assertFalse(noticed)
        self.assertEqual(visited, [watermark + 2, watermark + 1, watermark])

    def test_expired_custom_resumes_as_normal(self):
        start=time.time();store.set_policy('custom',limit=10,window_s=60,duration_s=5)
        store.set_policy('paused')
        with mock.patch.object(store.time,'time',return_value=start+6):
            self.assertEqual(store.resume_policy()['mode'],'normal')

    def test_malformed_and_future_epoch_fail_closed(self):
        store.set_policy('normal')
        value=json.loads((store.DATA_DIR/'policy.json').read_text());value['start_after_id']=999999
        (store.DATA_DIR/'policy.json').write_text(json.dumps(value))
        self.assertEqual(store.guard_status()['mode'],'wait')
        value['start_after_id']=0;value['version']=True
        (store.DATA_DIR/'policy.json').write_text(json.dumps(value))
        self.assertEqual(store.guard_status()['mode'],'wait')

    def test_invalid_policy_has_no_side_effect(self):
        for mode,kwargs in [('wrong',{}),(None,{}),([],{}),('normal',{'limit':5}),
                ('custom',{}),('custom',dict(limit=True,window_s=60,duration_s=60)),
                ('custom',dict(limit=0,window_s=60,duration_s=60)),
                ('custom',dict(limit=5,window_s=float('nan'),duration_s=60)),
                ('custom',dict(limit=5,window_s=60,duration_s=86401)),
                ('custom',dict(limit=5,window_s=60,duration_s=0))]:
            with self.assertRaises(ValueError):store.set_policy(mode,**kwargs)
        self.assertEqual(store.read_messages(),[])

    def test_durable_policy_survives_auxiliary_notice_failure(self):
        with mock.patch.object(store,'_write_record',side_effect=OSError('notice unavailable')):
            status=store.set_policy('paused')
        self.assertEqual(status['mode'],'paused')
        self.assertTrue(store.guard_status()['blocked'])

    def test_cli_custom_pause_resume(self):
        launcher=Path(store.__file__).resolve().parents[1]/'bin/agi'
        def command(*args):
            result=subprocess.run([str(launcher),*args],capture_output=True,text=True,check=True)
            return json.loads(result.stdout)
        self.assertEqual(command('custom','10','1','60')['limit'],10)
        self.assertEqual(command('pause')['mode'],'paused')
        self.assertEqual(command('resume')['mode'],'custom')
        self.assertIsNone(command('custom', '4', '1')['expires_at'])
        persisted = command('guard')
        self.assertEqual((persisted['mode'], persisted['limit'], persisted['expires_at']), ('custom', 4, None))
        normal = command('mode','normal')
        self.assertEqual((normal['mode'], normal['limit']), ('normal', 5))

    def test_shared_normal_rate_is_atomic_between_processes(self):
        launcher=Path(store.__file__).resolve().parents[1]/'bin/agi'
        children=[subprocess.Popen([str(launcher),'say','--from',store.AGENTS[i%2],str(i)],
                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) for i in range(12)]
        self.assertEqual(sorted(p.wait(timeout=15) for p in children),[0]*5+[1]*7)
        self.assertEqual(len([m for m in store.read_messages() if m['kind']=='msg']),5)
        self.assertEqual(len([m for m in store.read_messages() if m['text'].startswith(store._RATE_NOTICE)]),1)

    def test_policy_requires_current_mode_names(self):
        for mode in ('auto', 'quiet'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                store.set_policy(mode)

    def test_cli_rejects_removed_runtime_aliases_without_writing_state(self):
        launcher = Path(store.__file__).resolve().parents[1] / 'bin/agi'
        before = sorted(Path(self.directory.name).rglob('*'))
        for args in (['auto', '--seconds', '60'], ['status', '--as', 'codex', 'listening']):
            result = subprocess.run([str(launcher), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(sorted(Path(self.directory.name).rglob('*')), before)

if __name__ == '__main__':unittest.main()
