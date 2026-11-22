"""Codex session status and adapter refresh over the existing local Unix WebSocket. Owner: Codex."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time
import uuid


MAX_MESSAGE = 2 * 1024 * 1024


def session_id(value):
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('A Codex session UUID is required') from error
    if canonical != value:
        raise ValueError('Use the canonical lowercase Codex session UUID')
    return canonical


class CodexSession:
    def __init__(self, thread, *, socket_path=None, timeout=3):
        self.thread = session_id(thread)
        codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        self.path = socket_path or os.environ.get('AGI_CODEX_SOCKET') or codex_home / 'app-server-control/app-server-control.sock'
        self.timeout, self.sock, self.buffer, self.counter = timeout, None, b'', 0

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None
        self.buffer = b''

    def _read(self, size):
        while len(self.buffer) < size:
            data = self.sock.recv(65536)
            if not data:
                raise OSError('Codex local connection closed')
            self.buffer += data
        value, self.buffer = self.buffer[:size], self.buffer[size:]
        return value

    def _send(self, payload, opcode=1):
        mask, size = os.urandom(4), len(payload)
        if size < 126:
            header = bytes((128 | opcode, 128 | size))
        elif size <= 65535:
            header = bytes((128 | opcode, 254)) + struct.pack('!H', size)
        else:
            header = bytes((128 | opcode, 255)) + struct.pack('!Q', size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _receive(self, deadline):
        fragments, total, started = [], 0, False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Codex status request timed out')
            self.sock.settimeout(remaining)
            first, second = self._read(2)
            opcode, final, size = first & 15, bool(first & 128), second & 127
            if first & 112 or second & 128:
                raise OSError('Unsupported Codex WebSocket frame')
            if size == 126:
                size = struct.unpack('!H', self._read(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self._read(8))[0]
            if size > MAX_MESSAGE or total + size > MAX_MESSAGE:
                raise OSError('Codex status response exceeded the metadata limit')
            payload = self._read(size)
            if opcode == 8:
                raise OSError('Codex local server closed the connection')
            if opcode == 9:
                self._send(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not started:
                started = True
            elif opcode != 0 or not started:
                raise OSError('Unexpected Codex WebSocket message')
            fragments.append(payload)
            total += size
            if final:
                return json.loads(b''.join(fragments))

    def _request(self, method, params):
        self.counter += 1
        request_id = self.counter
        self._send(json.dumps(dict(id=request_id, method=method, params=params)).encode())
        deadline = time.monotonic() + self.timeout
        while True:
            value = self._receive(deadline)
            if not isinstance(value, dict):
                raise OSError('Invalid Codex JSON-RPC response')
            if value.get('id') != request_id:
                continue
            if 'error' in value:
                error = value['error']
                raise OSError(f"Codex {method} failed ({error.get('code')}): {error.get('message', '')[:200]}")
            return value['result']

    def _connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                          'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key + '\r\n'
                          'Sec-WebSocket-Version: 13\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buffer:
            data = self.sock.recv(4096)
            if not data:
                raise OSError('Codex WebSocket handshake closed')
            self.buffer += data
            if len(self.buffer) > 65536:
                raise OSError('Codex WebSocket header exceeded its limit')
        header, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        lines = header.decode('ascii').split('\r\n')
        fields = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
        fields = {k.lower(): v.strip() for k, v in fields.items()}
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if lines[0] != 'HTTP/1.1 101 Switching Protocols' or fields.get('sec-websocket-accept') != expected:
            raise OSError('Codex local WebSocket handshake was rejected')
        self._request('initialize', dict(clientInfo=dict(name='agi_bridge', version='1'),
                                         capabilities=dict(experimentalApi=True)))
        self._send(json.dumps(dict(method='initialized')).encode())

    def status(self):
        try:
            if self.sock is None:
                self._connect()
            thread = self._request('thread/read', dict(threadId=self.thread, includeTurns=False))['thread']
            if thread['id'] != self.thread:
                raise OSError('Codex returned a different session id')
            state = thread['status']['type']
            if state not in ('active', 'idle', 'notLoaded'):
                raise OSError('Codex session is not available: ' + str(state))
            return state
        except BaseException:
            self.close()
            raise

    def refresh_mcp(self):
        """Request a new MCP connection without opening or resuming a thread."""
        self.status()
        return self._request('config/mcpServer/reload', {})

    def withdraw_wake(self, wake_id):
        """Delete only queued submissions carrying this AGI notification ID."""
        if self.sock is None:
            self._connect()
        matches, cursors = [], set()
        params = dict(threadId=self.thread, limit=50)
        marker = f"AGI local watcher notification {wake_id}. This is the existing session,"
        while True:
            page = self._request('thread/queue/list', params)
            for item in page['data']:
                user_input = item['input']
                if (len(user_input) == 1 and user_input[0].get('type') == 'text'
                        and user_input[0].get('text', '').startswith(marker)):
                    matches.append(item['id'])
            cursor = page.get('nextCursor')
            if cursor is None:
                break
            if cursor in cursors:
                raise OSError('Codex repeated a queue cursor')
            cursors.add(cursor)
            params['cursor'] = cursor
        for queued_id in matches:
            self._request('thread/queue/delete', dict(threadId=self.thread, queuedSubmissionId=queued_id))
