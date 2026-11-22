"""Human UI state tests: separate windows, abrupt exit and durable recovery."""
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import store, ui_state

SCRATCH = os.environ.get('AGI_SCRATCH')


class UIStateTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='agi-state-',dir=SCRATCH)
        self.environment=mock.patch.dict(os.environ,AGI_DATA=self.folder.name)
        self.environment.start()
        importlib.reload(store)

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def test_recovery_preserves_chips_cursor_and_private_mode(self):
        marker = chr(ui_state.CHIP_FIRST + 3)
        text, chips = f'before {marker} after', {marker: 'large pasted payload'}
        with ui_state.DraftSession() as draft:
            draft.save(text, 7, chips, 3)
            identity=draft.session_id
            self.assertEqual(draft.path.stat().st_mode & 0o777,0o600)
        with ui_state.DraftSession() as recovered:
            self.assertEqual(recovered.session_id,identity)
            self.assertEqual(recovered.restored, {'draft': text, 'cursor': 7, 'chips': chips, 'chip_count': 3})
        self.assertEqual(store.get_ack('codex'),0)

    def test_live_windows_do_not_steal_each_others_drafts(self):
        first=ui_state.DraftSession();second=third=None
        try:
            first.save('first window',3)
            second=ui_state.DraftSession()
            self.assertIsNone(second.restored)
            self.assertNotEqual(first.session_id,second.session_id)
            second.save('second live window',5)
            first.close()
            third=ui_state.DraftSession()
            self.assertEqual(third.restored['draft'],'first window')
        finally:
            first.close()
            if second:second.close()
            if third:third.close()

    def test_process_exit_releases_lock_and_preserves_saved_draft(self):
        code='from agi.ui_state import DraftSession; import os; s=DraftSession(); s.save("before abrupt exit",6); os._exit(0)'
        env=dict(os.environ,PYTHONPATH=str(Path(store.__file__).resolve().parents[1]))
        subprocess.run([sys.executable,'-c',code],env=env,check=True,timeout=10)
        with ui_state.DraftSession() as recovered:
            self.assertEqual(recovered.restored['draft'],'before abrupt exit')

    def test_clearing_draft_removes_saved_payload(self):
        marker = chr(ui_state.CHIP_FIRST + 1)
        with ui_state.DraftSession() as draft:
            draft.save(marker, 1, {marker: 'x' * 10000}, 1)
            path=draft.path
        with ui_state.DraftSession() as recovered:
            recovered.save('',0)
            self.assertFalse(path.exists())
            self.assertIsNone(recovered.restored)
        with ui_state.DraftSession() as empty:self.assertIsNone(empty.restored)

    def test_corrupt_draft_is_preserved_and_reported(self):
        root=ui_state._directory()/'drafts';root.mkdir(exist_ok=True)
        bad=root/('a'*32+'.json');bad.write_text('invalid saved data')
        with ui_state.DraftSession() as fresh:
            self.assertIsNone(fresh.restored)
            self.assertEqual(len(fresh.warnings),1)
            self.assertEqual(bad.read_text(),'invalid saved data')

    def test_failed_atomic_save_keeps_previous_version(self):
        with ui_state.DraftSession() as draft:
            draft.save('saved before failure',5)
            with mock.patch.object(store,'_atomic_write',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):draft.save('new unsaved text',2)
        with ui_state.DraftSession() as recovered:
            self.assertEqual(recovered.restored['draft'],'saved before failure')

    def test_reading_position_is_private_and_independent_of_the_read_marker(self):
        store.append_message('user', 'first')
        store.append_message('user', 'second')
        ui_state.mark_read(2)

        position = ui_state.ReadingPosition()
        position.save(None)
        self.assertFalse(position.path.exists())
        position.save(1)
        self.assertEqual(position.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(ui_state.ReadingPosition().anchor, 1)
        self.assertEqual(ui_state.get_read_marker(), 2)

        original = position.path.read_bytes()
        with mock.patch.object(store, '_atomic_json') as write:
            position.save(1)
        write.assert_not_called()
        self.assertEqual(position.path.read_bytes(), original)
        position.save(None)
        self.assertIsNone(ui_state.ReadingPosition().anchor)
        self.assertEqual(store.get_ack('codex'), 0)

    def test_a_stale_window_cannot_overwrite_a_newer_position(self):
        first, stale = ui_state.ReadingPosition(), ui_state.ReadingPosition()
        first.save(5)
        stale.save(7)
        self.assertEqual(ui_state.ReadingPosition().anchor, 5)
        ui_state.ReadingPosition().save(None)
        stale.save(9)
        self.assertIsNone(ui_state.ReadingPosition().anchor)

    def test_invalid_position_is_ignored_and_failed_save_keeps_the_previous_one(self):
        position = ui_state.ReadingPosition()
        position.path.write_bytes(b'invalid position')
        self.assertIsNone(ui_state.ReadingPosition().anchor)
        self.assertEqual(position.path.read_bytes(), b'invalid position')
        position.save(4)
        with mock.patch.object(store, '_atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                position.save(9)
        self.assertEqual(ui_state.ReadingPosition().anchor, 4)

    def test_human_marker_is_monotonic_and_separate_from_agent_ack(self):
        for index in range(10):store.append_message('user',str(index))
        self.assertEqual(ui_state.get_read_marker(),0)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(ui_state.mark_read,[10,5]))
        self.assertEqual(ui_state.get_read_marker(),10)
        self.assertEqual(ui_state.mark_read(2),10)
        self.assertEqual(store.get_ack('codex'),0)
        self.assertEqual(store.get_ack('claude'),0)
        with self.assertRaises(ValueError):ui_state.mark_read(11)

    def test_marker_corruption_and_invalid_payloads_are_not_overwritten(self):
        root=ui_state._directory();path=root/'read.json';path.write_text('null')
        with self.assertRaisesRegex(ValueError,'Invalid human'):ui_state.get_read_marker()
        self.assertEqual(path.read_text(),'null')
        with ui_state.DraftSession() as draft:
            with self.assertRaises(ValueError):draft.save('abc',4)
            with self.assertRaises(ValueError):draft.save('abc',True)
            with self.assertRaises(ValueError):draft.save('abc',1,{'label':False})

    def test_send_clear_failure_does_not_restore_a_committed_draft(self):
        with ui_state.DraftSession() as draft:
            draft.save('sent before cleanup failure',6)
            with mock.patch.object(draft,'save',side_effect=OSError('cleanup unavailable')):
                message=draft.send('sent before cleanup failure','sent before cleanup failure',6)
            self.assertEqual(message['id'],1)
            self.assertTrue(draft.path.exists())
        with ui_state.DraftSession() as recovered:self.assertIsNone(recovered.restored)
        self.assertEqual(len(store.read_messages()),1)

    def test_uncommitted_pending_send_restores_and_retries_same_token(self):
        with ui_state.DraftSession() as draft:
            with mock.patch.object(store,'append_message',side_effect=OSError('before append')):
                with self.assertRaises(OSError):draft.send('safe retry','safe retry',4)
            token=json.loads(draft.path.read_text())['pending_send']['request_id']
        with ui_state.DraftSession() as recovered:
            self.assertEqual(recovered.restored['draft'],'safe retry')
            message=recovered.send('safe retry','safe retry',4)
            self.assertEqual(message['request_id'],token)
        self.assertEqual(len(store.read_messages()),1)

    def test_new_identical_draft_is_not_a_retry_after_cleanup_failure(self):
        with ui_state.DraftSession() as draft:
            with mock.patch.object(draft,'save',side_effect=OSError('cleanup unavailable')):
                first=draft.send('same text','same text',4)
            draft.save('same text',4)
            self.assertNotIn('pending_send',json.loads(draft.path.read_text()))
            second=draft.send('same text','same text',4)
            self.assertNotEqual(first['request_id'],second['request_id'])
        self.assertEqual(len(store.read_messages()),2)

    def test_pending_draft_survives_uncertain_log_confirmation(self):
        with ui_state.DraftSession() as draft:
            with mock.patch.object(draft,'save',side_effect=OSError('cleanup unavailable')):
                draft.send('already committed','already committed',5)
        with mock.patch.object(store,'find_request',side_effect=OSError('temporary read error')):
            with ui_state.DraftSession() as recovered:
                self.assertEqual(recovered.restored['draft'],'already committed')
                self.assertTrue(recovered.warnings)
                with mock.patch.object(store, '_atomic_json', side_effect=OSError('disk unavailable')):
                    with self.assertRaises(OSError):
                        recovered.save('edit that did not save', 3)
                recovered.send('already committed','already committed',5)
        self.assertEqual(len(store.read_messages()),1)

    def test_prepublic_label_keyed_draft_is_refused_and_preserved(self):
        with ui_state.DraftSession() as draft:
            draft.save('before [old label]', 6)
            path = draft.path
        saved = json.loads(path.read_text())
        saved.update(chips={'[old label]': 'pasted text'}, chip_count=1)
        original = json.dumps(saved)
        path.write_text(original)
        with ui_state.DraftSession() as recovered:
            self.assertIsNone(recovered.restored)
            self.assertIn('unsupported paste-chip format', recovered.warnings[0])
        self.assertEqual(path.read_text(), original)


if __name__=='__main__':unittest.main()
