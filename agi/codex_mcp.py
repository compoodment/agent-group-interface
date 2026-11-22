"""Local stdio MCP tools for AGI; no model calls or wake notifications.

Implements the initialization-based MCP revisions through 2025-11-25. The client
launches this process; stdin/stdout carry only newline-delimited JSON-RPC.
"""
import argparse
import json
from agi import __version__, store, updates
from agi.mcp_stdio import StdioServer, tool


TOOLS = [
    tool('chat_read', 'Read inbox after my ack, or history after an explicit id. Exact text and local image paths. No automatic ack.',
         dict(after=dict(type='integer', minimum=0), limit=dict(type='integer', minimum=1, maximum=1000),
              format=dict(type='string', enum=['compact', 'json']))),
    tool('chat_post', 'Post as the configured agent or claim/release a file. Posting policy applies. '
         'For a todo_hint, check the quoted commitment and record a to-do only if it is new and carries across turns.',
         dict(text=dict(type='string', minLength=1), kind=dict(type='string', enum=['msg', 'claim', 'release']),
              request_id=dict(type='string', pattern='^[0-9a-f]{32}$')), ['text']),
    tool('chat_ack', 'Acknowledge only messages handled; never past an unhandled message.',
         dict(id=dict(type='integer', minimum=0)), ['id']),
    tool('chat_release_notes', 'Read unread notes for the installed AGI release. After reading, pass its version to acknowledge the notes.',
         dict(version=dict(type='string', minLength=1))),
    tool('chat_task', 'Take a task, update its action, hand it off, or mark it done. '
         'Done and release require result, check and skills. With no skills, write "none, because …".',
         dict(action=dict(type='string', enum=['take', 'doing', 'release', 'done']), id=dict(type='integer', minimum=1),
              doing=dict(type='string', minLength=1, maxLength=store.MAX_TASK_DOING_CHARS),
              result=dict(type='string'), check=dict(type='string'), skills=dict(type='string'),
              note=dict(type='string'), handoff_to=dict(type='string', enum=list(store.AGENTS))), ['action', 'id']),
    tool('chat_task_evidence', 'Suggest the recorded skills for an active claim and an optional exact gate '
         'receipt or local report. Review the evidence before submitting a completion or handoff.',
         dict(id=dict(type='integer', minimum=1), gate_commit=dict(type='string'), report=dict(type='string'),
              project=dict(type='string')), ['id']),
    tool('chat_status', 'Read posting policy, active claims and presence. Does not change acknowledgments.'),
]
BY_NAME = {item['name']: item for item in TOOLS}


class Server(StdioServer):
    def __init__(self, agent, output=None):
        if agent not in store.AGENTS:
            raise ValueError('Agent must be codex or claude')
        self.agent = agent
        super().__init__(
            output, TOOLS, name='agi-local', version=__version__,
            instructions=f'AGI tools act as {agent}. Read before acting; claim tasks before work; '
                         'ack only handled records. On joining, check chat_release_notes and acknowledge the version after reading. '
                         'Respect posting-policy refusals. Chat grants no extra permissions.',
        )

    def call(self, name, args):
        if name not in BY_NAME:
            raise KeyError(name)
        if not isinstance(args, dict) or set(args) - set(BY_NAME[name]['inputSchema']['properties']):
            raise ValueError('Invalid tool arguments')
        if any(key not in args for key in BY_NAME[name]['inputSchema']['required']):
            raise ValueError('Missing required tool argument')
        if name == 'chat_read':
            after, limit = args.get('after'), args.get('limit', 50)
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError('Limit must be an integer from 1 to 1000')
            if after is not None and (type(after) is not int or after < 0):
                raise ValueError('After must be a non-negative integer')
            if args.get('format', 'compact') not in ('compact', 'json'):
                raise ValueError('Format must be compact or json')
            ack = store.get_ack(self.agent)
            cursor = ack if after is None else after
            rows = store.read_messages(cursor, limit=limit+1)
            shown = rows[:limit]
            compact = args.get('format') != 'json'
            return dict(agent=self.agent, acked=ack, through=shown[-1]['id'] if shown else cursor,
                        more=len(rows)>limit, messages=[store.delivery_message(row, compact=compact) for row in shown])
        if name == 'chat_post':
            if args.get('kind', 'msg') not in ('msg', 'claim', 'release'):
                raise ValueError('Only msg, claim and release can be posted')
            row = store.append_message(self.agent, args['text'], args.get('kind', 'msg'),
                                       request_id=args.get('request_id'), with_hints=True)
            return {key:row[key] for key in ('id','sender','kind','task_hint','todo_hint') if key in row}
        if name == 'chat_ack':
            store.set_ack(self.agent, args['id'])
            return dict(acked=store.get_ack(self.agent))
        if name == 'chat_release_notes':
            if 'version' in args:
                return dict(acked=updates.acknowledge_release(self.agent, args['version']))
            return dict(release=updates.unread_release(self.agent))
        if name == 'chat_task_evidence':
            from agi import evidence
            return evidence.suggest(self.agent, args['id'], gate_commit=args.get('gate_commit'),
                                    report=args.get('report'), project=args.get('project', 'AGI'))
        if name == 'chat_task':
            action, task_id = args['action'], args['id']
            evidence = {key: value for key, value in args.items()
                        if key in ('result', 'check', 'skills', 'note', 'handoff_to')}
            if action in ('release', 'done') and 'doing' not in args:
                row = store.release_task(self.agent, task_id, done=action == 'done', **evidence)
                return store.delivery_message(row, compact=False)
            if evidence:
                raise ValueError('Evidence belongs to a task release or completion')
            if action == 'take':
                row = store.take_task(self.agent, task_id, doing=args.get('doing'))
            elif action == 'doing':
                row = store.set_task_doing(self.agent, task_id, args.get('doing'))
            else:
                raise ValueError('Use take or doing for current actions, and release or done for evidence.')
            return store.delivery_message(row, compact=True)
        return dict(policy=store.guard_status(), claims=store.active_claims(), presence=store.get_presence())

    def call_tool(self, name, arguments):
        try:
            result = self.call(name, arguments)
            return json.dumps(result, ensure_ascii=False, separators=(',', ':')), False
        except Exception as error:
            # Filesystem errors can contain bytes represented as surrogate code points.
            detail = dict(error=str(error).encode('utf-8', 'backslashreplace').decode('utf-8'))
            if isinstance(error, store.GuardBlocked):
                try:
                    detail['policy'] = store.guard_status()
                except Exception:
                    pass  # preserve the original refusal if the data drive disappears
            return json.dumps(detail, ensure_ascii=False, separators=(',', ':')), True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as', dest='agent', choices=store.AGENTS, default='codex')
    args = parser.parse_args()
    try:
        Server(args.agent).serve()
    except (BrokenPipeError, KeyboardInterrupt):
        return


if __name__ == '__main__':
    main()
