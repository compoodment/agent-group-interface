"""Event-driven waking of one existing Codex session. Owner: Codex; stdlib only."""
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import time
import uuid

from agi import membership, reminders, store
from agi.codex_client import CodexSession, session_id

RETRY_SECONDS = 30
MAX_ATTEMPTS = 3
ACTIVITY_REFRESH = 10
ACTIVITY_TTL = 35
IO_RETRY_SECONDS = 10
# Merely closing an O_RDWR flock handle emits CLOSE_WRITE even without a write.
# Watch real modifications/renames, excluding CLOSE_WRITE to avoid waking ourselves.
WATCH_MASK = 0x00000FC6


def _directory():
    store._prepare()
    path = store.DATA_DIR / 'bridge'
    created = not path.exists()
    path.mkdir(mode=0o700, exist_ok=True)
    if created:
        store._sync_directory(store.DATA_DIR)
    return path


def _state():
    try:
        value = json.loads((_directory() / 'codex.json').read_text())
    except FileNotFoundError:
        return dict(version=1, enabled=False, thread=None, pid=None, pending=None,
                    status='stopped', error='', probe_requested=None, probe_success=None)
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value.get('version') != 1 or
            type(value.get('enabled')) is not bool or not isinstance(value.get('error'), str)):
        raise ValueError('Invalid Codex watcher state; preserved for inspection')
    if value['enabled'] and value.get('thread') is None:
        raise ValueError('Enabled watcher has no session')
    if value.get('thread') is not None:
        session_id(value['thread'])
    if value.get('probe_requested') is not None:
        store._request_id(value['probe_requested'])
    withdrawals = value.get('withdrawals', [])
    if not isinstance(withdrawals, list):
        raise ValueError('Invalid Codex wake withdrawals')
    for entry in withdrawals:
        if (not isinstance(entry, dict) or set(entry) != {'thread', 'id', 'retry_at'}
                or not store._finite_nonnegative_number(entry['retry_at'])):
            raise ValueError('Invalid Codex wake withdrawal')
        session_id(entry['thread'])
        store._request_id(entry['id'])
    pending = value.get('pending')
    if pending is not None:
        if (not isinstance(pending, dict) or pending.get('phase') not in ('prepared', 'queued', 'failed')
                or pending.get('kind') not in ('messages', 'probe')):
            raise ValueError('Invalid pending Codex wake; preserved for inspection')
        store._integer(pending.get('through'), 'wake position')
        store._integer(pending.get('attempts'), 'wake attempts')
        store._request_id(pending.get('id'))
        if pending.get('revision') is not None:
            store._request_id(pending['revision'])
        retry_at = pending.get('retry_at')
        if type(retry_at) not in (int, float) or not math.isfinite(retry_at) or retry_at < 0:
            raise ValueError('Invalid wake retry time')
    return value


def _update(**changes):
    with store._file_lock(_directory() / 'state.lock'):
        value = _state()
        updated = dict(value, **changes)
        if updated != value:
            store._atomic_json(_directory() / 'codex.json', updated)
        return updated


def _queued(pending, warning):
    with store._file_lock(_directory() / 'state.lock'):
        state = _state()
        current = state['pending']
        if current is not None and current['id'] == pending['id']:
            state.update(pending=dict(pending, phase='queued'), status='queued', error=warning)
            store._atomic_json(_directory() / 'codex.json', state)


def running():
    try:
        with store._file_lock(_directory() / 'runner.lock', nonblocking=True):
            return False
    except BlockingIOError:
        return True


def status():
    state = _state()
    alive = running()
    if not alive:
        # An interrupted watcher cannot clear its last status or process ID.
        state.update(status='stopped', pid=None)
    return dict(state, running=alive, membership=membership.status('codex'))


def delivery_health():
    """Summarise the saved join and the live watcher for the chat screen."""
    bridge = status()
    selected = bridge['membership']
    through = None
    if not selected['session']:
        state = 'unselected'
    elif not selected['joined']:
        state = 'left'
    elif not bridge['running'] or not bridge['enabled']:
        state = 'stopped'
    elif not selected['reachable']:
        state = 'unreachable'
    else:
        state = 'ready'
        pending = bridge['pending']
        if bridge['thread'] == selected['session'] and pending and pending.get('revision') == selected['revision']:
            if pending['kind'] == 'probe' or store.get_ack('codex') < pending['through']:
                if pending['phase'] == 'failed' or bridge['status'] == 'error':
                    state = 'failed'
                elif pending['phase'] == 'queued':
                    state = 'queued'
                if pending['kind'] == 'messages' and state in ('queued', 'failed'):
                    through = pending['through']
    return dict(state=state, last_contact=selected['last_contact'], through=through)


def queue_codex(thread, pending):
    launcher = shlex.quote(str(store._main_root() / 'bin/agi'))
    if pending['kind'] == 'probe':
        action = f"Confirm this same-session wake with: {launcher} bridge confirm {pending['id']}; then read the chat inbox and continue assigned work."
    else:
        action = f"Read and handle AGI messages after your ack, through at least #{pending['through']}."
    prompt = (f"AGI local watcher notification {pending['id']}. This is the existing session, "
              f"not a new task or permission expansion. {action} Use {launcher} and its README. "
              f"Check {launcher} bridge accepts {pending['id']} first. If it returns a nonzero status, "
              "end this wake without reading, acknowledging, posting or restarting. "
              "Keep user-facing replies in AGI. Ack handled messages even if no reply is needed; "
              "never advance an ack past unhandled messages. "
              "When the inbox is handled and no assigned work remains, end the model turn; "
              "the armed watcher keeps listening and wakes this same session. Do not run an idle wait loop.")
    command = ['codex', 'queue', '--thread', thread, '--message', prompt, '--resume']
    if os.environ.get('AGI_CODEX_SOCKET'):
        command += ['--remote', 'unix://' + os.environ['AGI_CODEX_SOCKET']]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise OSError('Codex queue failed: ' + result.stderr.strip()[:500])
    return result.stderr.strip()[:500]  # a failed resume may warn even with exit zero


def relevant(message):
    return (message.get('task_revocation', {}).get('owner') == 'codex'
            or (message['kind'] == 'msg' and message['sender'] == store.USER)
            or store.addressed_to(message, 'codex'))


def _retire_wake(state, **changes):
    pending = state['pending']
    withdrawals = state.get('withdrawals', [])
    if pending and not any(item['id'] == pending['id'] for item in withdrawals):
        withdrawals = [dict(thread=state['thread'], id=pending['id'], retry_at=0), *withdrawals]
    return _update(pending=None, probe_requested=None, withdrawals=withdrawals, **changes)


def _reconcile_withdrawal(state, now, open_session):
    entry = next((item for item in state.get('withdrawals', []) if item['retry_at'] <= now), None)
    if entry is None:
        return
    session = open_session(entry['thread'])
    warning = ''
    try:
        session.withdraw_wake(entry['id'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        warning = f"Could not withdraw the old Codex wake: {error}"[:500]
    finally:
        session.close()
    with store._file_lock(_directory() / 'state.lock'):
        state = _state()
        pending = state.get('withdrawals', [])
        if entry not in pending:
            return
        pending.remove(entry)
        if warning:
            pending.append(dict(entry, retry_at=now + RETRY_SECONDS))
        state.update(withdrawals=pending, withdrawal_error=warning)
        store._atomic_json(_directory() / 'codex.json', state)


class Controller:
    def __init__(self, session, *, enqueue=queue_codex, clock=time.time, open_session=CodexSession):
        self.session, self.enqueue, self.clock = session, enqueue, clock
        self.open_session = open_session
        self.heartbeat, self.native_state = float('-inf'), None

    def _discard_wake(self, state, label, **changes):
        retired = _retire_wake(state, status=label, error='', **changes)
        _reconcile_withdrawal(retired, self.clock(), lambda thread: self._wake_session(thread, state['thread']))

    def _wake_session(self, thread, current):
        return self.session if thread == current else self.open_session(thread)

    def tick(self):
        state, now = _state(), self.clock()
        if not state['enabled']:
            return None
        if state.get('withdrawals'):
            _reconcile_withdrawal(state, now, lambda thread: self._wake_session(thread, state['thread']))
            state = _state()
        selected = membership.status('codex')
        if not selected['joined']:
            if state['pending'] or state.get('probe_requested') or state['status'] != 'left':
                self._discard_wake(state, 'left')
            if now-self.heartbeat >= ACTIVITY_REFRESH:
                # Keep observing the selected terminal so the user can resume it.
                # This is a metadata read and cannot queue a model turn.
                self.heartbeat = now
                with suppress(OSError, ValueError, KeyError):
                    if self.session.status() != 'notLoaded':
                        membership.observe('codex', state['thread'], ttl=ACTIVITY_TTL)
                self.session.close()
            return ACTIVITY_REFRESH
        if selected['session'] != state['thread']:
            session_id(selected['session'])
            self._discard_wake(state, 'listening', thread=selected['session'])
            self.session.close()
            self.session = self.open_session(selected['session'])
            self.heartbeat, self.native_state = float('-inf'), None
            return 0
        ack = store.get_ack('codex')
        pending = state['pending']
        revision = selected['revision']
        if pending and pending.get('revision') != revision:
            self._discard_wake(state, 'listening')
            return 0
        if pending and pending['kind'] == 'messages' and ack >= pending['through']:
            state = _update(pending=None, error='')
            pending = None
        unread = [m for m in store.read_messages(ack) if relevant(m)]
        if pending and pending['phase'] == 'failed' and any(
                m['sender'] == store.USER and m['id'] > pending['through'] for m in unread):
            state = _update(pending=None, error='')
            pending = None  # new user input reopens delivery; older unread messages are retained
        policy = store.guard_status()
        if policy['blocked']:
            self._refresh_presence(now, state['thread'])
            self.session.close()
            label = 'paused' if policy['mode'] == 'paused' else 'rate_limited'
            _update(status=label)
            return (min(ACTIVITY_REFRESH, max(.1, policy['retry_after']))
                    if policy['retry_after'] is not None else ACTIVITY_REFRESH)
        if not unread and not pending and not state.get('probe_requested'):
            native = self._refresh_presence(now, state['thread'])
            _update(status='working' if native == 'active' else 'listening')
            self.session.close()  # no unattended WebSocket ping/timeout while the chat is silent
            return ACTIVITY_REFRESH
        if pending and pending['phase'] == 'failed':
            self._refresh_presence(now, state['thread'])
            self.session.close()
            return ACTIVITY_REFRESH
        native = self.session.status()
        self._observe(native, now, state['thread'])
        if native == 'active':
            _update(status='working')
            return 1
        # The session can acknowledge work or the user can pause/stop the
        # watcher while the native status request is in flight.
        current = _state()
        if not current['enabled']:
            self.session.close()
            return None
        if (current['pending'] != pending or current.get('probe_requested') != state.get('probe_requested')
                or store.get_ack('codex') != ack or store.guard_status()['blocked']
                or not membership.can_deliver('codex', state['thread'], revision=revision)):
            self.session.close()
            return 0
        if pending is None:
            probe = state.get('probe_requested')
            pending = dict(id=probe or uuid.uuid4().hex, kind='probe' if probe else 'messages',
                           through=ack if probe else unread[-1]['id'], attempts=0,
                           phase='prepared', retry_at=now, revision=revision)
        if now < pending.get('retry_at', 0):
            self.session.close()
            return min(ACTIVITY_REFRESH, pending['retry_at'] - now)
        if pending['attempts'] >= MAX_ATTEMPTS:
            _update(pending=dict(pending, phase='failed'), status='error',
                    error='Wake ended without acknowledgment; use bridge retry or rejoin')
            return ACTIVITY_REFRESH
        # Persist intent before the external side effect. A restart retains the
        # same wake id; ack/native-state checks prevent normal duplicate work.
        pending = dict(pending, attempts=pending['attempts']+1, phase='prepared', retry_at=now+RETRY_SECONDS)
        with store._locked(exclusive=True):
            current = _state()
            if (not current['enabled'] or current['thread'] != state['thread']
                    or not membership.can_deliver('codex', state['thread'], revision=revision)
                    or current['pending'] != state['pending']
                    or current.get('probe_requested') != state.get('probe_requested')):
                return 0
            _update(pending=pending, probe_requested=None)
        try:
            warning = self.enqueue(state['thread'], pending)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.session.close()
            _update(status='error', error=str(error)[:500])
            return 0 if not membership.can_deliver('codex', state['thread'], revision=revision) else ACTIVITY_REFRESH
        queued_at = time.time()
        _queued(pending, warning)
        if not membership.can_deliver('codex', state['thread'], revision=revision):
            return 0  # The next tick reconciles the in-flight native submission.
        if pending['kind'] == 'messages':
            through = pending['through']
            first = next((row['id'] for row in unread if row['id'] <= through), through)
            with suppress(OSError, ValueError):
                store.woke('codex', through, first_id=first, queued_at=queued_at)
        return 1

    def _refresh_presence(self, now, thread):
        if now-self.heartbeat >= ACTIVITY_REFRESH:
            self._observe(self.session.status(), now, thread)
        return self.native_state

    def _observe(self, native, now, thread):
        if native != self.native_state or now-self.heartbeat >= ACTIVITY_REFRESH:
            if native != 'notLoaded':
                membership.observe('codex', thread, ttl=ACTIVITY_TTL)
            store.set_reachable('codex', ttl=ACTIVITY_TTL)
            store.set_presence('codex', 'working' if native == 'active' else 'idle', ttl=ACTIVITY_TTL)
            self.heartbeat, self.native_state = now, native


def _lock_identity(stream):
    fd = stream.fileno()
    stat = os.fstat(fd)
    # A different mount of the same drive can expose the same device and inode.
    try:
        with open(f'/proc/self/fdinfo/{fd}') as info:
            mount = next((line for line in info if line.startswith('mnt_id:')), None)
    except FileNotFoundError:
        mount = None
    return stat.st_dev, stat.st_ino, mount


def _current_runner(lease, path):
    store._prepare()
    try:
        current = path.open('rb')
    except FileNotFoundError:
        return False
    with current:
        return _lock_identity(lease) == _lock_identity(current)


def run():
    runner = _directory() / 'runner.lock'
    with store._file_lock(runner, nonblocking=True) as lease:
        state = _state()
        if not state['enabled']:
            return
        controller, watch = Controller(CodexSession(state['thread'])), None
        _update(pid=os.getpid(), status='listening')
        try:
            while True:
                try:
                    if watch is None:
                        if not _current_runner(lease, runner):
                            break
                        watch = store._inotify((store.CHAT_DIR, store.CURSOR_DIR, store.DATA_DIR, _directory()),
                                               event_mask=WATCH_MASK)
                    delay = controller.tick()
                    if delay is None:
                        break
                    delay = reminders.wait_limit(delay)
                    if watch is None:
                        time.sleep(min(delay, 1))
                    elif select.select([watch], [], [], delay)[0]:
                        lost = False
                        try:
                            while True:
                                events = os.read(watch, 65536)
                                if not events:
                                    lost = True
                                    break
                                lost |= store._watch_lost(events)
                        except BlockingIOError:
                            pass
                        if lost:
                            os.close(watch)
                            watch = None
                except (OSError, ValueError, KeyError) as error:
                    controller.session.close()
                    if watch is not None:
                        with suppress(OSError):
                            os.close(watch)
                        watch = None
                    # Error diagnostics can fail for the same reason as the tick.
                    # Keep the lease and try again without moving the stored inbox.
                    try:
                        if not _current_runner(lease, runner):
                            break
                        _update(status='error', error=str(error)[:500])
                    except (OSError, ValueError, KeyError):
                        pass
                    time.sleep(IO_RETRY_SECONDS)
        finally:
            controller.session.close()
            if watch is not None:
                with suppress(OSError):
                    os.close(watch)
            try:
                if _current_runner(lease, runner):
                    _update(pid=None, status='stopped')
                    store.clear_presence('codex')
            except (OSError, ValueError, KeyError):
                pass  # The lock still closes if Storage cannot record the exit.


def start(thread=None):
    thread = session_id(thread or os.environ.get('CODEX_THREAD_ID'))
    with store._file_lock(_directory() / 'manager.lock'):
        # Verify the existing endpoint before publishing readiness. No model call.
        session = CodexSession(thread)
        try:
            if session.status() == 'notLoaded':
                raise ValueError('Open the chosen Codex session before joining.')
        finally:
            session.close()
        membership.observe('codex', thread, ttl=ACTIVITY_TTL)
        previous, selected = membership._request_join('codex', thread)
        if running() or selected['session'] != thread:
            return status()
        return _launch_joined(thread, selected, previous)


def _launch_joined(thread, selected, previous):
    try:
        return _launch_locked(thread)
    except (OSError, ValueError):
        membership._undo_failed_join('codex', selected, previous)
        raise


def _launch_locked(thread):
    previous = _state()
    if previous['thread'] not in (None, thread):
        retired = _retire_wake(previous)
        _reconcile_withdrawal(retired, time.time(), CodexSession)
    _update(enabled=True, thread=thread, pid=None, status='starting', error='')
    try:
        fd = os.open(_directory() / 'codex.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'ab') as log:
            child = subprocess.Popen([sys.executable, '-m', 'agi.bridge'],
                     cwd=store.CHECKOUT_ROOT, env=dict(os.environ, PYTHONPATH=str(store.CHECKOUT_ROOT)),
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except (OSError, ValueError):
        _update(enabled=False, status='stopped', error='Watcher could not start; inspect bridge/codex.log')
        raise
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        state = status()
        if state['running'] and state['pid'] == child.pid:
            return state
        if child.poll() is not None:
            break
        time.sleep(.025)
    _update(enabled=False, error='Watcher failed to start; inspect bridge/codex.log')
    raise OSError('Watcher failed to start; inspect bridge/codex.log')


def _stop_locked():
    _update(enabled=False)
    deadline = time.monotonic()+20
    while running() and time.monotonic() < deadline:
        time.sleep(.025)
    if running():
        raise OSError('Watcher is still stopping; inspect bridge status')


def stop():
    with store._file_lock(_directory() / 'manager.lock'):
        membership.leave('codex')
        _stop_locked()
        retired = _retire_wake(_state(), error='')
        _reconcile_withdrawal(retired, time.time(), CodexSession)
        return status()


def resume(session=None):
    """Honor a user-selected Codex session, starting its watcher if necessary."""
    with store._file_lock(_directory() / 'manager.lock'):
        if running():
            selected = membership.resume('codex', session)
            _retry_pending(failed_revision=selected['revision'])
            return selected
        previous = membership.status('codex')
        thread = session or previous['session']
        if thread is None:
            raise ValueError("No Codex session has joined. Join from the existing Codex session.")
        client = CodexSession(thread)
        try:
            if client.status() == 'notLoaded':
                raise ValueError("The selected Codex session is closed. Open it and join again.")
        finally:
            client.close()
        membership.observe('codex', thread, ttl=ACTIVITY_TTL)
        previous, result = membership._resume('codex', session, expected_session=thread)
        _launch_joined(result['session'], result, previous)
        return result


def refresh():
    """Refresh an enabled live bridge on its own thread, preserving any pending wake."""
    with store._file_lock(_directory() / 'manager.lock'):
        previous = status()
        if not previous['enabled'] or not previous['running']:
            return previous
        thread = previous['thread']
        session = CodexSession(thread)
        try:
            session.refresh_mcp()
        finally:
            session.close()
        if previous['membership']['session'] is None:
            # Adopt the exact thread of a watcher already started by the user
            # before session membership was available.
            membership.observe('codex', thread, ttl=ACTIVITY_TTL)
            selected = membership.request_join('codex', thread)
            if previous['pending'] and selected['session'] == thread:
                _update(pending=dict(previous['pending'], revision=selected['revision']))
        _stop_locked()
        return _launch_locked(thread)


def _retry_pending(*, failed_revision=None):
    # Match the delivery lock order while comparing the pending wake with the ack.
    with store._locked(), store._file_lock(_directory() / 'state.lock'):
        state = _state()
        pending = state['pending']
        if not pending or not _accepts(state, pending['id'], state['thread']):
            return
        if pending['kind'] == 'messages' and store._ack('codex') >= pending['through']:
            return
        if failed_revision is not None and (pending.get('revision') != failed_revision or
                                            (pending['phase'] != 'failed' and state['status'] != 'error')):
            return
        state.update(pending=dict(pending, attempts=0, phase='prepared', retry_at=0), error='')
        store._atomic_json(_directory() / 'codex.json', state)


def retry():
    _retry_pending()
    return status()


def probe():
    if not running():
        raise ValueError('Start the watcher before requesting a probe')
    with store._file_lock(_directory() / 'state.lock'):
        state = _state()
        if not state['enabled'] or not membership.can_deliver('codex', state['thread']):
            raise ValueError('Join the Codex session before requesting a probe')
        if state['pending']:
            raise ValueError('Finish the outstanding wake before probing')
        if not state.get('probe_requested'):
            state.update(probe_requested=uuid.uuid4().hex, probe_success=None)
            store._atomic_json(_directory() / 'codex.json', state)
        return state


def _accepts(state, wake_id, caller):
    pending = state['pending']
    return (state['enabled'] and caller == state['thread'] and pending is not None
            and pending['id'] == wake_id and membership.can_deliver('codex', caller, revision=pending.get('revision')))


def accepts(wake_id):
    store._request_id(wake_id)
    state = _state()
    if not running() or not _accepts(state, wake_id, os.environ.get('CODEX_THREAD_ID')):
        return False
    return state['pending']['kind'] == 'probe' or store.get_ack('codex') < state['pending']['through']


def confirm(wake_id):
    store._request_id(wake_id)
    caller = os.environ.get('CODEX_THREAD_ID')
    with store._file_lock(_directory() / 'state.lock'):
        state = _state()
        pending = state['pending']
        if caller != state['thread']:
            raise ValueError('Probe confirmation must come from the configured existing Codex session')
        if not _accepts(state, wake_id, caller) or pending['kind'] != 'probe':
            raise ValueError('No matching enabled same-session probe')
        state.update(pending=None, probe_success=dict(id=wake_id, thread=caller, at=time.time()), error='')
        store._atomic_json(_directory() / 'codex.json', state)
        return state['probe_success']


if __name__ == '__main__':
    run()
