"""Shared newline-delimited MCP transport for AGI's native clients."""

import json
import sys
import threading

SUPPORTED = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_REQUEST = 8 * 1024 * 1024


def tool(name, description, properties=None, required=()):
    schema = dict(type='object', properties=properties or {}, required=list(required),
                  additionalProperties=False)
    return dict(name=name, description=description, inputSchema=schema)


class StdioServer:
    def __init__(self, output, tools, *, name, version, instructions, experimental=None,
                 fallback=SUPPORTED[0], require_version=True):
        self.output = sys.stdout if output is None else output
        self._write_lock = threading.Lock()
        self.initialized = False
        self.tools = tools
        self._tool_names = {description['name'] for description in tools}
        self._fallback = fallback
        self._require_version = require_version
        capabilities = {'tools': {}}
        if experimental is not None:
            capabilities['experimental'] = experimental
        self._handshake = dict(capabilities=capabilities, serverInfo=dict(name=name, version=version),
                               instructions=instructions)

    def send(self, message):
        data = json.dumps(message, separators=(',', ':'), allow_nan=False) + '\n'
        with self._write_lock:
            self.output.write(data)
            self.output.flush()

    def error(self, ident, code, message):
        self.send(dict(jsonrpc='2.0', id=ident, error=dict(code=code, message=message)))

    def on_initialized(self):
        """Start any provider-specific notifications after the handshake."""

    def handle(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return self.error(None, -32600, 'Invalid request')
        method = message['method']
        if 'id' not in message:
            if self.initialized and method == 'notifications/initialized':
                self.on_initialized()
            return
        ident = message['id']
        if type(ident) not in (str, int):
            return self.error(None, -32600, 'Invalid request id')
        if isinstance(ident, str):
            try:
                ident.encode('utf-8')
            except UnicodeEncodeError:
                return self.error(None, -32600, 'Invalid request id')
        params = message.get('params', {})
        if not isinstance(params, dict):
            return self.error(ident, -32602, 'Params must be an object')
        if method == 'initialize':
            asked = params.get('protocolVersion')
            if self._require_version and not isinstance(asked, str):
                return self.error(ident, -32602, 'Missing protocolVersion')
            self.initialized = True
            value = dict(self._handshake, protocolVersion=asked if asked in SUPPORTED else self._fallback)
        elif method == 'ping':
            value = {}
        elif not self.initialized:
            return self.error(ident, -32002, 'Initialize first')
        elif method == 'tools/list':
            if params.get('cursor'):
                return self.error(ident, -32602, 'Invalid tools cursor')
            value = dict(tools=self.tools)
        elif method == 'tools/call':
            name = params.get('name')
            if not isinstance(name, str) or name not in self._tool_names:
                return self.error(ident, -32602, 'Unknown tool')
            arguments = params.get('arguments')
            text, failed = self.call_tool(name, {} if arguments is None else arguments)
            value = dict(content=[dict(type='text', text=text)], isError=failed)
        else:
            return self.error(ident, -32601, 'Method not found')
        self.send(dict(jsonrpc='2.0', id=ident, result=value))

    def serve(self, source=None):
        source = sys.stdin.buffer if source is None else source
        while raw := source.readline(MAX_REQUEST + 1):
            if len(raw) > MAX_REQUEST:
                self.error(None, -32700, 'Request too large')
                return
            if not raw.strip():
                continue
            try:
                message = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
            except (ValueError, RecursionError):
                self.error(None, -32700, 'Parse error')
                continue
            self.handle(message)
