"""Independent end-to-end recovery check. Run: python3 tests/recovery_probe.py bin/agi.

Real UI, isolated Storage data, deliberate SIGKILL of test children only. Not a hardware power cut.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import tempfile
import time
from terminal_probe import Probe


def until(probe, predicate, timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():return True
        probe.collect(.04)
    return predicate()


def marker(root):
    path=root/'ui/read.json'
    return json.loads(path.read_text())['read_through'] if path.exists() else 0


def chat(launcher,root,*args):
    result=subprocess.run([str(launcher),*args],env=dict(os.environ,AGI_DATA=str(root)),
                          capture_output=True,text=True,check=True,timeout=10)
    return json.loads(result.stdout)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('launcher',type=Path);args=parser.parse_args()
    launcher=args.launcher.resolve();results={}
    with tempfile.TemporaryDirectory(prefix='agi-recovery-',dir=os.environ.get('AGI_SCRATCH')) as directory:
        root=Path(directory);first=second=third=None
        try:
            first=Probe(launcher,root)
            prefix='Please review:\n';payload='    '+'x'*1200+'\n        last line'
            first.send(b'Please review:\n')
            first.send(b'\x1b[200~'+payload.encode()+b'\x1b[201~')
            first.send(b'\x01')                 # move left to the chip's logical line boundary
            def saved():
                for path in (root/'ui/drafts').glob('*.json'):
                    value=json.loads(path.read_text())
                    if value['cursor']==len(prefix) and payload in value['chips'].values():return True
                return False
            results['draft_saved_before_crash']=until(first,saved)
            first.process.kill();first.process.wait(timeout=3)
            first.close();first=None
            second=Probe(launcher,root)
            results['restoration_note_visible']=b'Restored your unsent draft' in second.output
            second.send(b'EDIT ')
            second.send(b'\r')
            messages=second.messages()
            results['recovered_cursor_and_full_chip_sent_once']=len(messages)==1 and messages[0]['text']==prefix+'EDIT '+payload
            results['successful_send_clears_saved_draft']=until(second,lambda:not list((root/'ui/drafts').glob('*.json')))
            second.close();second=None
            third=Probe(launcher,root)
            third.send(b'fresh\r')
            results['next_restart_has_no_ghost_draft']=len(third.messages())==2 and third.messages()[-1]['text']=='fresh'
        finally:
            for probe in (first,second,third):
                if probe:probe.close()
    # Deliberately exit at each side of the actual UI append, after its intent is saved.
    for committed in (False, True):
        with tempfile.TemporaryDirectory(prefix='agi-send-boundary-',dir=os.environ.get('AGI_SCRATCH')) as directory:
            root=Path(directory);data=root/'data';data.mkdir()
            wrapper=root/'crash-chat'
            wrapper.write_text("#!/usr/bin/python3\nimport sys, os\nsys.path.insert(0, "+repr(str(launcher.parent.parent))+")\nfrom agi import store\noriginal=store.append_message\ndef crash(*args, **kwargs):\n    "+("original(*args, **kwargs)" if committed else "pass")+"\n    os._exit(0)\nstore.append_message=crash\nfrom agi.cli import main\nmain()\n")
            wrapper.chmod(0o700)
            probe=Probe(wrapper,data)
            try:
                probe.send(b'boundary intent\r')
                probe.process.wait(timeout=4)
                records=probe.messages()
                results[f'boundary_{committed}_correct_commit_count']=len(records)==int(committed)
            finally:probe.close()
            probe=Probe(launcher,data)
            try:
                restored=b'Restored your unsent draft' in probe.output
                results[f'boundary_{committed}_correct_restore']=restored is (not committed)
                probe.send(b'fresh after commit\r' if committed else b'\r')
                records=probe.messages()
                expected=['boundary intent','fresh after commit'] if committed else ['boundary intent']
                results[f'boundary_{committed}_exactly_once']=[m['text'] for m in records]==expected
            finally:probe.close()
    with tempfile.TemporaryDirectory(prefix='agi-read-marker-',dir=os.environ.get('AGI_SCRATCH')) as directory:
        root=Path(directory);probe=Probe(launcher,root)
        try:
            one=chat(launcher,root,'say','--from','codex','@user first background message')
            probe.collect(.25)
            results['background_arrival_not_marked_read']=marker(root)==0
            probe.send(b'x')
            two=chat(launcher,root,'say','--from','codex','@user second background message')
            until(probe,lambda:marker(root)>=one['id'])
            results['debounced_read_marker_uses_interaction_snapshot']=marker(root)==one['id']
            before_exit=marker(root)
            probe.stop(signal.SIGTERM)
            results['exit_does_not_mark_new_background_arrival_read']=marker(root)==before_exit
        finally:probe.close()
    Terminal=runpy.run_path(str(Path(__file__).with_name('test_ui.py')))['Terminal']
    def rendered(probe):
        terminal=Terminal(24,80)
        terminal.feed(bytes(probe.output).decode('utf-8','replace'))
        return terminal.text()
    with tempfile.TemporaryDirectory(prefix='agi-history-review-',dir=os.environ.get('AGI_SCRATCH')) as directory:
        root=Path(directory);(root/'chat').mkdir()
        with (root/'chat/2026-11-16.jsonl').open('w') as stream:
            for number in range(1,2001):
                text='ordinary history '+str(number)
                if number==10:text='ancientneedle '+'full context words '*20+' OLD_MESSAGE_END'
                stream.write(json.dumps({'id':number,'ts':'2026-11-16T11:00:00+01:00',
                                         'sender':'codex' if number==10 else 'user','kind':'msg','text':text})+'\n')
        probe=Probe(launcher,root)
        try:
            probe.send(b'/find ancientneedle\r')
            results['older_history_match_visible']=until(probe,lambda:'ancientneedle' in rendered(probe))
            if 'OLD_MESSAGE_END' not in rendered(probe):probe.send(b'\x1b[6~')
            results['older_message_readable_in_full']='OLD_MESSAGE_END' in rendered(probe)
            before_browsing_key=marker(root)
            chat(launcher,root,'say','--from','codex','LATEST_LIVE_SENTINEL')
            probe.collect(.25)
            view=rendered(probe)
            results['arrival_preserves_history_view']='ancientneedle' in view or 'OLD_MESSAGE_END' in view
            probe.collect(2.1)
            probe.send(b'\x1b[5~')
            results['history_keys_do_not_mark_unseen_live_arrivals']=marker(root)==before_browsing_key
            probe.send(b'\x1b')
            results['escape_returns_to_live_history']=until(probe,lambda:'LATEST_LIVE_SENTINEL' in rendered(probe))
            probe.send(b'back at latest\r')
            messages=probe.messages()
            results['browse_does_not_reset_live_cursor']=len(messages)==2002 and messages[-1]['text']=='back at latest'
        finally:probe.close()
    print(json.dumps(results,indent=2))
    if not all(results.values()):raise SystemExit(1)


if __name__=='__main__':main()
