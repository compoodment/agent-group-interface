"""Durable human read position and per-window drafts. Owner: Codex."""
import json
import math
import os
import re
import threading
import time
import uuid

from agi import store

VERSION = 1
SESSION_ID = re.compile(r'[0-9a-f]{32}')


def _directory():
    store._prepare()
    root = store.DATA_DIR / 'ui'
    created = not root.exists()
    root.mkdir(exist_ok=True, mode=0o700)
    if created:
        store._sync_directory(store.DATA_DIR)
    return root


SETTING_ENV = {"notifications": "AGI_NOTIFY", "mouse": "AGI_MOUSE"}


def _saved_settings():
    try:
        data = json.loads((_directory() / "settings.json").read_text())
    except FileNotFoundError:
        return {}
    if (not isinstance(data, dict) or data.get("version") != VERSION
            or set(data) - {"version", *SETTING_ENV}
            or any(type(data[name]) is not bool for name in SETTING_ENV if name in data)):
        raise ValueError("Invalid AGI user settings.")
    return {name: data[name] for name in SETTING_ENV if name in data}


def user_settings():
    """Read the saved values and any explicit per-process overrides."""
    saved = _saved_settings()
    return {name: os.environ[env] != "0" if env in os.environ else saved.get(name, True)
            for name, env in SETTING_ENV.items()}


def set_user_setting(name, enabled):
    if name not in SETTING_ENV or type(enabled) is not bool:
        raise ValueError("Choose a setting and whether to turn it on.")
    directory = _directory()
    with store._file_lock(directory / "settings.lock"):
        saved = _saved_settings()
        saved[name] = enabled
        store._atomic_json(directory / "settings.json", dict(saved, version=VERSION))
    return user_settings()


def get_read_marker():
    path = _directory() / 'read.json'
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
            raise ValueError('unsupported marker version')
        return store._integer(value['read_through'], 'human read marker')
    except FileNotFoundError:
        return 0
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f'Invalid human read marker: {path}') from error


def mark_read(message_id):
    store._integer(message_id, 'human read marker')
    with store._file_lock(_directory() / 'read.lock'):
        current = get_read_marker()
        if message_id <= current:
            return current
        tail = store.read_tail(1)
        if not tail or message_id > tail[-1]['id']:
            raise ValueError('Human read marker cannot exceed the latest message')
        store._atomic_json(_directory() / 'read.json', {'version': VERSION, 'read_through': message_id})
        return message_id


def _read_position(path):
    try:
        with path.open('rb') as stream:
            data = stream.read(257)
        if len(data) > 256:
            return None
        record = json.loads(data)
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(record, dict) or set(record) != {'version', 'revision', 'message_id'}:
        return None
    version, revision, anchor = record['version'], record['revision'], record['message_id']
    if (type(version) is not int or version != VERSION
            or not isinstance(revision, str) or not SESSION_ID.fullmatch(revision)
            or (anchor is not None and (type(anchor) is not int or anchor < 1))):
        return None
    return record


class ReadingPosition:
    """Save the view from this window only if its opening revision is still current."""

    def __init__(self):
        self.path = _directory() / 'position.json'
        self._record = _read_position(self.path)

    @property
    def anchor(self):
        return self._record['message_id'] if self._record else None

    def save(self, message_id):
        if message_id is not None and (type(message_id) is not int or message_id < 1):
            raise ValueError('Reading position requires a positive message id')
        if message_id == self.anchor:
            return
        store._prepare()
        with store._file_lock(self.path.with_suffix('.lock')):
            if _read_position(self.path) != self._record:
                return
            record = {'version': VERSION, 'revision': uuid.uuid4().hex, 'message_id': message_id}
            store._atomic_json(self.path, record)
            self._record = record


CHIP_FIRST = 0xF0000


def is_chip(character):
    """Return whether this is a saved paste-chip marker, a single private-use character."""
    return len(character) == 1 and CHIP_FIRST <= ord(character) <= 0xFFFFD


def _payload(draft, cursor, chips, chip_count, *, attachments=None):
    if not isinstance(draft, str):
        raise ValueError('Draft must be text')
    store._integer(cursor, 'draft cursor')
    store._integer(chip_count, 'paste chip count')
    if cursor > len(draft):
        raise ValueError('Draft cursor is past the end of the text')
    chips = {} if chips is None else chips
    if not isinstance(chips, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                          for k, v in chips.items()):
        raise ValueError('Draft chips must map keys to text')
    if any(not is_chip(key) for key in chips):
        raise ValueError('unsupported paste-chip format')
    payload = {'draft': draft, 'cursor': cursor, 'chips': dict(chips), 'chip_count': chip_count}
    images = store._attachments(attachments)
    if images:
        payload['attachments'] = images
    return payload


def _saved_payload(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
        raise ValueError('unsupported draft version')
    updated = value.get('updated_at')
    if type(updated) not in (int, float) or not math.isfinite(updated):
        raise ValueError('invalid draft timestamp')
    payload = _payload(value['draft'], value['cursor'], value['chips'], value['chip_count'],
                       attachments=value.get('attachments'))
    pending = value.get('pending_send')
    if pending is not None:
        if not isinstance(pending, dict) or not isinstance(pending.get('text'), str):
            raise ValueError('invalid pending send')
        images = store._attachments(pending.get('attachments'))
        if images != payload.get('attachments', []) or (not images and not pending['text'].strip()):
            raise ValueError('invalid pending send')
        store._request_id(pending.get('request_id'))
        pending = dict(pending)
    return payload, pending


def _same_content(first, second):
    return (first['draft'] == second['draft'] and first['chips'] == second['chips']
            and first.get('attachments', []) == second.get('attachments', []))


class DraftSession:
    """Own one draft lock; recover an inactive window rather than another live one."""

    def __init__(self):
        self.restored = None
        self.warnings = []
        self._mutex = threading.RLock()
        root = _directory()
        self.directory = root / 'drafts'
        created = not self.directory.exists()
        self.directory.mkdir(exist_ok=True, mode=0o700)
        if created:
            store._sync_directory(root)
        self._lock = None
        self._pending = None
        candidates = []
        for path in self.directory.glob('*.json'):
            if SESSION_ID.fullmatch(path.stem):
                try:
                    candidates.append((path.stat().st_mtime_ns, path))
                except FileNotFoundError:
                    pass
        for _, path in sorted(candidates, reverse=True):
            try:
                lock = store._file_lock(path.with_suffix('.lock'), nonblocking=True)
            except BlockingIOError:
                continue
            except OSError as error:
                self.warnings.append(f'Saved draft {path.name} could not be locked; preserved ({type(error).__name__})')
                continue
            try:
                saved, pending = _saved_payload(path)
                if not saved['draft'] and not saved.get('attachments'):
                    lock.close()
                    continue
            except FileNotFoundError:
                lock.close()
                continue
            except (ValueError, TypeError, KeyError, OSError) as error:
                lock.close()
                detail = str(error) if isinstance(error, ValueError) else type(error).__name__
                self.warnings.append(f'Saved draft {path.name} could not be read; preserved ({detail})')
                continue
            if pending is not None:
                try:
                    committed = store.find_request(pending['request_id'])
                    if committed is not None:
                        if (committed['sender'] != store.USER or committed['text'] != pending['text']
                                or committed.get('attachments', []) != pending.get('attachments', [])):
                            raise ValueError('pending send does not match its committed record')
                        try:
                            path.unlink()
                            store._sync_directory(self.directory)
                        except OSError:
                            self.warnings.append('An already sent draft could not be cleaned up; it was not restored')
                        lock.close()
                        continue
                except (ValueError, OSError) as error:
                    self.warnings.append(f'Draft delivery could not be confirmed; preserved ({type(error).__name__})')
            self._pending = pending
            self.path, self._lock, self.restored = path, lock, saved
            break
        if self._lock is None:
            self.path = self.directory / (uuid.uuid4().hex + '.json')
            self._lock = store._file_lock(self.path.with_suffix('.lock'), nonblocking=True)
        self.session_id = self.path.stem
        self._last_payload = _payload(**self.restored) if self.restored else None

    def save(self, draft, cursor, chips=None, chip_count=0, *, attachments=None):
        payload = _payload(draft, cursor, chips, chip_count, attachments=attachments)
        with self._mutex:
            if self._lock is None:
                raise ValueError('Draft session is closed')
            store._prepare()
            if payload == self._last_payload:
                return
            pending = None
            if not draft and not payload.get('attachments'):
                try:
                    self.path.unlink()
                    store._sync_directory(self.directory)
                except FileNotFoundError:
                    pass
            else:
                record = dict(payload, version=VERSION, updated_at=time.time())
                if self._pending is not None and self._last_payload and _same_content(payload, self._last_payload):
                    pending = record['pending_send'] = self._pending
                store._atomic_json(self.path, record)
            self._pending = pending
            self._last_payload = payload
            self.restored = None

    def send(self, text, draft, cursor, chips=None, chip_count=0, *, attachments=None):
        """Journal the intent before append; a committed retry never becomes a second message."""
        payload = _payload(draft, cursor, chips, chip_count, attachments=attachments)
        images = payload.get('attachments', [])
        if not isinstance(text, str) or (not text.strip() and not images):
            raise ValueError('Message text must not be empty')
        if not draft and not images:
            raise ValueError('Sending requires a nonempty saved draft')
        with self._mutex:
            if self._lock is None:
                raise ValueError('Draft session is closed')
            store._prepare()
            pending = self._pending
            if pending is None or pending['text'] != text or pending.get('attachments', []) != images:
                pending = {'request_id': uuid.uuid4().hex, 'text': text}
                if images:
                    pending['attachments'] = images
            record = dict(payload, version=VERSION, updated_at=time.time(), pending_send=pending)
            store._atomic_json(self.path, record)
            self._pending, self._last_payload, self.restored = pending, payload, None
            message = store.append_message(store.USER, text, request_id=pending['request_id'], attachments=images)
            try:
                self.save('', 0)
            except OSError:
                # Disk still has a recognizable committed intent. In memory,
                # however, the next draft is a new intent even if its text is
                # identical; force its next save rather than reusing this token.
                self._pending = self._last_payload = self.restored = None
            return message

    def close(self):
        with self._mutex:
            if self._lock is not None:
                self._lock.close()
                self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
