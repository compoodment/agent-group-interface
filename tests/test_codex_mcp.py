"""Local MCP transport and CLI-equivalent store behavior; isolated data only."""
import importlib
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = os.environ.get('AGI_SCRATCH')
sys.path.insert(0, str(ROOT))
from agi import codex_mcp, store


TOOL_NAMES = {"chat_read", "chat_post", "chat_ack", "chat_release_notes", "chat_task",
              "chat_task_evidence", "chat_status"}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agi-mcp-', dir=SCRATCH)
        self.env = patch.dict(os.environ, AGI_DATA=self.temp.name)
        self.env.start();importlib.reload(store)
        self.server = codex_mcp.Server('codex', io.StringIO())

    def tearDown(self):
        self.env.stop();self.temp.cleanup()

    def test_pagination_preserves_exact_text_control_records_and_ack(self):
        text = 'héllo\n"quotes"\tand unicode 🟣'
        rows = [store.append_message('user', text), store.append_message('claude','README.md','claim')]
        first = self.server.call('chat_read', {'limit':1})
        self.assertEqual(first['messages'], [[rows[0][k] for k in ('id','sender','kind','text')]])
        self.assertTrue(first['more']);self.assertEqual(first['through'],rows[0]['id'])
        self.assertEqual(store.get_ack('codex'),0)
        self.server.call('chat_ack', {'id':first['through']})
        second = self.server.call('chat_read', {})
        self.assertEqual(second['messages'][0][2], 'claim');self.assertFalse(second['more'])
        self.assertEqual(self.server.call('chat_read',{'after':0})['messages'][0][3],text)
        self.assertEqual(store.get_ack('codex'),rows[0]['id'])
        self.assertEqual(self.server.call('chat_read',{'after':0,'format':'json'})['messages'],rows)

    def test_read_keeps_watcher_presence_when_inbox_is_empty(self):
        lease = store.set_presence('codex', 'working', 'Watcher saw an active turn')
        self.assertEqual(self.server.call('chat_read', {})['messages'], [])
        self.assertEqual(store.get_presence()['codex'], lease)

    def test_sender_bound_guard_claims_and_tasks_match_store(self):
        request = store.append_message('user','please work')
        self.server.call('chat_task',{'action':'take','id':request['id'],'doing':'Checking the release'})
        self.assertEqual(store.list_tasks()[0]['doing'], 'Checking the release')
        self.server.call('chat_task',{'action':'doing','id':request['id'],'doing':'Verifying the download'})
        self.assertEqual(store.list_tasks()[0]['doing'], 'Verifying the download')
        with self.assertRaises(store.ClaimConflict):store.take_task('claude',request['id'])
        args = dict(action='release', id=request['id'], result='Checked the candidate', check='Manual download check')
        with self.assertRaisesRegex(ValueError, 'requires skills'):
            self.server.call('chat_task', args)
        with self.assertRaisesRegex(ValueError, 'because'):
            self.server.call('chat_task', dict(args, skills='none'))
        with self.assertRaisesRegex(ValueError, 'record the task'):
            self.server.call('chat_post', dict(text=f"task:{request['id']}", kind='release'))
        self.assertEqual(store.list_tasks()[0]['owner'], 'codex')
        proof = dict(args, skills='none, because this is a synthetic download')
        release = self.server.call('chat_task', proof)
        self.assertEqual(self.server.call('chat_task', proof), release)
        self.assertEqual(release['check'], args['check'])
        self.server.call('chat_task', dict(action='take', id=request['id']))
        completion = self.server.call('chat_task', dict(proof, action='done', skills='release-readiness'))
        self.assertTrue(completion['done'])
        self.server.call('chat_post',{'text':'agi','kind':'claim'})
        with self.assertRaises(store.ClaimConflict):store.append_message('claude','agi/ui','claim')
        self.server.call('chat_post',{'text':'agi','kind':'release'})
        for i in range(5):self.assertEqual(self.server.call('chat_post',{'text':str(i)})['sender'],'codex')
        with self.assertRaises(store.GuardBlocked):self.server.call('chat_post',{'text':'over budget'})
        policy = self.server.call('chat_status', {})['policy']
        self.assertTrue(policy['blocked'])
        self.assertEqual((policy['limit'], policy['used']), (5, 5))

    def test_reply_to_another_agents_claim_reports_current_action(self):
        request = store.append_message('user', 'Rename the repositories')
        task_id = request['id']
        store.take_task('claude', task_id, doing='Renaming the GitHub repositories')
        args = dict(text=f'↪ #{task_id} I can verify the links.', request_id='a' * 32)
        posted = self.server.call('chat_post', args)
        self.assertEqual(posted['task_hint'], dict(id=task_id, owner='claude', doing='Renaming the GitHub repositories'))
        self.assertNotIn('task_hint', store.read_messages(posted['id'] - 1, limit=1)[0])
        self.assertEqual(self.server.call('chat_post', args), posted)
        store.set_task_doing('claude', task_id, 'Verifying the old URLs')
        updated = self.server.call('chat_post', args)
        self.assertEqual(updated['id'], posted['id'])
        self.assertEqual(updated['task_hint']['doing'], 'Verifying the old URLs')
        store.release_task('claude', task_id, result='Checked the request',
                           check='Read the fixture', skills='none, because this is a synthetic request')
        self.assertNotIn('task_hint', self.server.call('chat_post', dict(text=f'↪ #{task_id} The links work.')))

    def test_task_evidence_previews_the_claim_and_leaves_it_active(self):
        task = store.append_message('user', 'Check a synthetic example')
        claim = store.take_task('codex', task['id'])
        evidence = self.server.call('chat_task_evidence', dict(id=task['id']))
        self.assertEqual(evidence, dict(claim_id=claim['id'], skills=[], check=None))
        self.assertEqual(store.task_claim(task['id'])['claim_id'], claim['id'])

    def test_validation_idempotency_and_future_ack(self):
        args={'text':'one message','request_id':'a'*32}
        first=self.server.call('chat_post',args)
        self.assertEqual(self.server.call('chat_post',args),first)
        self.assertEqual(len(store.read_messages()),1)
        for name,args in [('chat_post',{'text':'x','sender':'user'}),('chat_post',{'text':'x','kind':'system'}),
                          ('chat_read',{'limit':True}),('chat_read',{'after':-1}),('chat_ack',{'id':999}),
                          ('chat_task',{'action':'unknown','id':1}),('chat_post',{})]:
            with self.subTest(name=name,args=args),self.assertRaises(ValueError):self.server.call(name,args)
        self.assertEqual(store.get_ack('codex'),0)

    def test_future_commitment_is_advice_in_the_post_receipt(self):
        request = store.append_message('user', 'Check the release')
        candidate = f"↪ #{request['id']} Once the gate lands, I’ll check the installed build."
        args = dict(text=candidate, request_id='b' * 32)
        posted = self.server.call('chat_post', args)
        self.assertEqual(posted['todo_hint'], dict(source_id=request['id'], candidate=candidate))
        self.assertEqual(self.server.call('chat_post', args), posted)
        self.assertNotIn('todo_hint', store.read_messages(posted['id'] - 1, limit=1)[0])
        self.assertEqual(store.list_todos(), [])

        posted = self.server.call('chat_post', {'text': "I’ll remove the Mini folder after review."})
        self.assertEqual(posted['todo_hint']['source_id'], posted['id'])
        candidate = "The page is live now. I’ll check the phone layout later."
        posted = self.server.call('chat_post', {'text': candidate})
        self.assertEqual(posted['todo_hint']['candidate'], candidate)

    def test_quotes_current_work_and_proposals_are_not_commitments(self):
        text = "\n".join([
            "> I’ll write the report tomorrow.",
            "```text",
            "I’ll write the report tomorrow.",
            "```",
            "`I’ll write the report tomorrow.`",
            "    I’ll write the report tomorrow.",
            "I’ll check the file now.",
            "I will check the log in this turn.",
            'The message was "The page is live. I’ll check the phone layout later."',
            'The message was “The page is live. I’ll check the phone layout later.”',
            "I would check the package if we agree.",
            "Next: check the package?",
            "I checked the package.",
        ])
        self.assertNotIn('todo_hint', self.server.call('chat_post', {'text': text}))
        user_post = store.append_message('user', "I’ll check it tomorrow.", with_hints=True)
        self.assertNotIn('todo_hint', user_post)

    def test_cli_retry_after_mcp_post_returns_the_original_receipt(self):
        text = 'same message after losing the MCP receipt 🟣\nsecond line'
        request_id = '13' * 16
        original = self.server.call('chat_post', dict(text=text, request_id=request_id))
        retry = subprocess.run([str(ROOT / 'bin/agi'), 'say', '--from', 'codex',
                                '--request-id', request_id, text], capture_output=True,
                               text=True, check=True, timeout=10)
        receipt = json.loads(retry.stdout)
        self.assertEqual(receipt['id'], original['id'])
        self.assertEqual(receipt['text'], text)
        self.assertEqual(store.read_messages(), [receipt])

    def test_protocol_recovers_after_invalid_json_and_unknown_requests(self):
        request=lambda i,m,p={}:json.dumps(dict(jsonrpc='2.0',id=i,method=m,params=p)).encode()+b'\n'
        data=(b'not-json\n'+request(1,'tools/list')+
              request(2,'initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}})+
              b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'+request(3,'tools/list')+
              request(4,'tools/call',{'name':'unknown'})+request(5,'tools/call',{'name':'chat_ack','arguments':{'id':999}})+
              request(6,'ping'))
        self.server.serve(io.BytesIO(data))
        replies=[json.loads(line) for line in self.server.output.getvalue().splitlines()]
        self.assertEqual([r.get('error',{}).get('code') for r in replies[:2]],[-32700,-32002])
        self.assertEqual(replies[2]['result']['protocolVersion'],'2025-11-25')
        self.assertEqual({tool['name'] for tool in replies[3]['result']['tools']}, TOOL_NAMES)
        self.assertEqual(replies[4]['error']['code'],-32602)
        self.assertTrue(replies[5]['result']['isError'])
        self.assertEqual(replies[6]['result'],{})
        self.assertEqual(store.get_ack('codex'),0)

    def test_null_arguments_and_unexpected_tool_error_keep_server_alive(self):
        self.server.initialized = True
        request = lambda i: dict(jsonrpc='2.0', id=i, method='tools/call',
                                 params=dict(name='chat_status', arguments=None))
        with patch.object(store, 'get_presence', side_effect=RuntimeError('drive vanished')):
            self.server.handle(request(1))
        self.server.handle(request(2))
        replies = [json.loads(line) for line in self.server.output.getvalue().splitlines()]
        self.assertTrue(replies[0]['result']['isError'])
        self.assertIn('drive vanished', replies[0]['result']['content'][0]['text'])
        self.assertFalse(replies[1]['result']['isError'])

    def test_decoder_failure_rejects_one_frame_and_keeps_connection(self):
        ping = b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        valid = json.loads(ping)
        with patch.object(codex_mcp.json, 'loads', side_effect=[RecursionError('decoder depth'), valid]):
            self.server.serve(io.BytesIO(b'first frame\n' + ping))
        bad, good = map(json.loads, self.server.output.getvalue().splitlines())
        self.assertEqual(bad['error']['code'], -32700)
        self.assertEqual(good['result'], {})

    def test_tool_error_with_filesystem_bytes_stays_on_utf8_wire(self):
        output = io.BytesIO()
        with io.TextIOWrapper(output, encoding='utf-8') as stream:
            server = codex_mcp.Server('codex', stream)
            server.initialized = True
            filename = os.fsdecode(b'file-\xff')
            with patch.object(store, 'get_presence', side_effect=OSError(filename)):
                server.handle(dict(jsonrpc='2.0', id=1, method='tools/call',
                                   params=dict(name='chat_status')))
            server.handle(dict(jsonrpc='2.0', id=2, method='ping'))
            error, ping = map(json.loads, output.getvalue().splitlines())
        self.assertTrue(error['result']['isError'])
        detail = json.loads(error['result']['content'][0]['text'])
        self.assertIn(b'file-', detail['error'].encode('utf-8'))
        self.assertEqual(ping['result'], {})

    def test_real_stdio_process_only_emits_protocol_and_exits_on_eof(self):
        env=dict(os.environ,PYTHONPATH=str(ROOT),PYTHONDONTWRITEBYTECODE='1')
        proc=subprocess.Popen([sys.executable,'-P','-m','agi.codex_mcp','--as','codex'],cwd=self.temp.name,env=env,
                              stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def request(ident,method,params):
            proc.stdin.write(json.dumps(dict(jsonrpc='2.0',id=ident,method=method,params=params))+'\n');proc.stdin.flush()
            self.assertTrue(select.select([proc.stdout],[],[],5)[0],'server did not respond')
            reply=json.loads(proc.stdout.readline());self.assertEqual(reply['id'],ident);return reply['result']
        try:
            request(1,'initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}})
            self.assertEqual({tool['name'] for tool in request(2,'tools/list',{})['tools']}, TOOL_NAMES)
            result=request(3,'tools/call',{'name':'chat_post','arguments':{'text':'stdio\nexact'}})
            self.assertFalse(result['isError']);self.assertEqual(json.loads(result['content'][0]['text'])['sender'],'codex');self.assertEqual(store.read_messages()[-1]['text'],'stdio\nexact')
            proc.stdin.close();self.assertEqual(proc.wait(5),0)
            self.assertEqual(proc.stdout.read(),'');self.assertEqual(proc.stderr.read(),'')
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
            for pipe in (proc.stdin,proc.stdout,proc.stderr):
                if not pipe.closed:pipe.close()

if __name__=='__main__':unittest.main()
