"""PNG attachments across storage, draft recovery and participant delivery."""

import importlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import zlib

from agi import claude_channel, codex_mcp, png_attachment, store, ui_state


SCRATCH = os.environ.get('AGI_SCRATCH')


def chunk(kind, data):
    payload = kind + data
    return struct.pack('!I', len(data)) + payload + struct.pack('!I', zlib.crc32(payload))


def png(red=255, width=1, height=1):
    header = struct.pack('!IIBBBBB', width, height, 8, 6, 0, 0, 0)
    pixel = bytes([0, red, 0, 0, 255])
    return (png_attachment.SIGNATURE + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(pixel)) + chunk(b'IEND', b''))


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='agi-attachments-', dir=SCRATCH)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.data = self.root / 'chat data'
        self.enterContext(mock.patch.dict(os.environ, AGI_DATA=str(self.data)))
        importlib.reload(store)

    def test_png_validation_and_deduplication(self):
        source = self.root / 'a screenshot.png'
        source.write_bytes(png())
        ref = store.attach(source)
        self.assertEqual(store.attach(png()), ref)
        stored = store.attachment_path(ref)
        self.assertEqual((ref['width'], ref['height']), (1, 1))
        self.assertEqual(stored.read_bytes(), source.read_bytes())
        self.assertEqual(list(stored.parent.iterdir()), [stored])
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, 'must be a PNG'):
            store.attach(b'text')
        with self.assertRaisesRegex(ValueError, 'Incomplete PNG'):
            store.attach(png()[:-5])
        with self.assertRaisesRegex(ValueError, 'million pixels'):
            store.attach(png(width=4000, height=4001))
        oversized = self.root / 'oversized.png'
        with oversized.open('wb') as stream:
            stream.truncate(png_attachment.MAX_BYTES + 1)
        with self.assertRaisesRegex(ValueError, '16 MiB'):
            store.attach(oversized)

    def test_retries_include_the_attachment_list_and_reads_are_detached(self):
        ref = store.attach(png())
        token = 'a' * 32
        args = dict(request_id=token, attachments=[ref])
        message = store.append_message('user', '', **args)
        self.assertEqual(store.append_message('user', '', **args), message)
        row = store.read_messages()[0]
        row['attachments'][0]['width'] = 20
        self.assertEqual(store.read_messages(), [message])
        with self.assertRaisesRegex(ValueError, 'different message content'):
            store.append_message('user', '', request_id=token, attachments=[store.attach(png(1))])
        with self.assertRaisesRegex(ValueError, 'Invalid image attachment path'):
            store.append_message('user', '', attachments=[dict(ref, path='../outside.png')])
        with self.assertRaisesRegex(ValueError, 'dimensions differ'):
            store.append_message('user', '', attachments=[dict(ref, height=2)])
        with self.assertRaisesRegex(ValueError, 'up to 4'):
            store.append_message('user', '', attachments=[ref] * 5)
        with self.assertRaises(FileNotFoundError):
            store.append_message('user', '', attachments=[dict(ref, path=f"attachments/{'0' * 64}.png")])
        self.assertEqual(store.last_id(), message['id'])

    def test_image_only_draft_can_recover_and_send_once(self):
        ref = store.attach(png())
        with ui_state.DraftSession() as draft:
            with mock.patch.object(store, 'append_message', side_effect=OSError('before append')):
                with self.assertRaises(OSError):
                    draft.send('', '', 0, attachments=[ref])
            pending = json.loads(draft.path.read_text())['pending_send']
        with ui_state.DraftSession() as draft:
            self.assertEqual(draft.restored['attachments'], [ref])
            message = draft.send('', '', 0, attachments=[ref])
        self.assertEqual(message['request_id'], pending['request_id'])
        self.assertEqual(store.read_messages(), [message])
        with ui_state.DraftSession() as draft:
            self.assertIsNone(draft.restored)

    def test_cli_and_both_providers_deliver_local_paths(self):
        source = self.root / 'source.png'
        source.write_bytes(png())
        launcher = Path(__file__).resolve().parents[1] / 'bin' / 'agi'
        output = subprocess.check_output([launcher, 'say', '--from', 'user', '--attach', source],
                                         text=True, timeout=10)
        posted = json.loads(output)
        message = store.read_tail(1)[0]
        local_ref = dict(message['attachments'][0], path=str(store.attachment_path(message['attachments'][0])))
        self.assertEqual(posted['attachments'], [local_ref])
        self.assertEqual(posted['text'], '')
        output = subprocess.check_output([launcher, 'wait', '--as', 'codex', '--timeout', '0', '--compact'],
                                         text=True, timeout=10)
        self.assertEqual(json.loads(output), [posted['id'], 'user', 'msg', '', [local_ref]])
        mcp = codex_mcp.Server('codex', io.StringIO())
        self.assertEqual(mcp.call('chat_read', {})['messages'][0][-1], [local_ref])
        self.assertEqual(mcp.call('chat_read', {'format': 'json'})['messages'], [posted])
        output = io.StringIO()
        claude_channel.Channel(output).notify([message])
        notification = json.loads(output.getvalue())
        self.assertIn(local_ref['path'], notification['params']['content'])
        self.assertEqual(store.get_ack('codex'), 0)


if __name__ == '__main__':
    unittest.main()
