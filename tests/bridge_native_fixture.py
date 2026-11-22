"""Isolated Unix native endpoint and queue executable; never invokes an installed agent."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading


@contextmanager
def socket_address(directory, name):
    """Keep the socket in scratch while giving local child processes a short address."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield Path(f'/proc/{os.getpid()}/fd/{descriptor}') / name
    finally:
        os.close(descriptor)


class NativeFixture:
    def __init__(self, directory, thread, socket_path):
        self.root, self.thread = Path(directory), thread
        self.path = socket_path
        self.active = self.root / 'native-active'
        self.queue_log = self.root / 'queue.jsonl'
        self.errors, self.reads, self.deleted = [], [], []
        self.refreshes = 0
        self.stopped = threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path)); self.server.listen(); self.server.settimeout(.1)
        self.clients = set()
        self.worker = threading.Thread(target=self.serve, daemon=True)
        self.worker.start()
        self.bin = self.root / 'bin'; self.bin.mkdir()
        executable = self.bin / 'codex'
        executable.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
root = Path(os.environ['FAKE_NATIVE_ROOT'])
(root / 'native-active').touch()
with (root / 'queue.jsonl').open('a') as output:
    output.write(json.dumps(sys.argv[1:]) + '\\n')
''')
        executable.chmod(0o700)

    def queues(self):
        try:
            return [json.loads(line) for line in self.queue_log.read_text().splitlines()]
        except FileNotFoundError:
            return []

    def serve(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.clients.add(conn)
            try:
                self.connection(conn)
            except (ConnectionError, socket.timeout):
                pass
            except BaseException as error:
                if not self.stopped.is_set(): self.errors.append(repr(error))
            finally:
                self.clients.discard(conn); conn.close()

    def connection(self, conn):
        conn.settimeout(3)
        header = b''
        # Exact header reads leave any later WebSocket bytes on the socket.
        while not header.endswith(b'\r\n\r\n'):
            chunk = conn.recv(1)
            if not chunk: return
            header += chunk
        key = next(line.split(b':',1)[1].strip() for line in header.split(b'\r\n')
                   if line.lower().startswith(b'sec-websocket-key:'))
        accept = base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
        def read(count):
            data = b''
            while len(data) < count:
                chunk = conn.recv(count-len(data))
                if not chunk: raise ConnectionError('closed')
                data += chunk
            return data
        while not self.stopped.is_set():
            first, length = read(2); count = length & 127
            if count == 126: count = struct.unpack('!H',read(2))[0]
            elif count == 127: count = struct.unpack('!Q',read(8))[0]
            if not length & 128 or count > 65536: raise ValueError('invalid client frame')
            mask = read(4); raw = read(count)
            request = json.loads(bytes(b ^ mask[i%4] for i,b in enumerate(raw)))
            if 'id' not in request: continue
            if request['method'] == 'initialize': result = {}
            elif request['method'] == 'thread/read':
                if request['params'] != dict(threadId=self.thread,includeTurns=False):
                    raise ValueError('wrong session or non-metadata request')
                self.reads.append('active' if self.active.exists() else 'idle')
                result = dict(thread=dict(id=self.thread,status=dict(type=self.reads[-1])))
            elif request['method'] == 'config/mcpServer/reload' and request['params'] == {}:
                self.refreshes += 1
                result = {}
            elif request['method'] == 'thread/queue/list':
                if request['params']['threadId'] != self.thread:
                    raise ValueError('wrong queue session')
                rows = [dict(id=str(number), input=[dict(type='text', text=args[args.index('--message') + 1])])
                        for number, args in enumerate(self.queues(), 1) if str(number) not in self.deleted]
                rows.append(dict(id='unrelated', input=[dict(type='text', text='A manually queued message.')]))
                result = dict(data=rows, nextCursor=None)
            elif request['method'] == 'thread/queue/delete':
                if request['params']['threadId'] != self.thread:
                    raise ValueError('wrong queue session')
                self.deleted.append(request['params']['queuedSubmissionId'])
                result = dict(deleted=True)
            else: raise ValueError('unexpected native method')
            data = json.dumps(dict(id=request['id'],result=result)).encode()
            head = bytes((129,len(data))) if len(data)<126 else bytes((129,126))+struct.pack('!H',len(data))
            conn.sendall(head+data)

    def close(self):
        self.stopped.set()
        for conn in tuple(self.clients):
            try: conn.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        self.server.close(); self.worker.join(4)
