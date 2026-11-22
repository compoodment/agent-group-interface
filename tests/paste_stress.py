"""Focused investigation of the intermittent large-chip send; isolated real PTYs.

Run: python3 tests/paste_stress.py bin/agi --rounds 30 --store-load.
Prints exact write sizes and timings; saves failed fixture/terminal/trace evidence to --report.
"""
import argparse
import codecs
import json
import os
from pathlib import Path
import runpy
import statistics
import subprocess
import tempfile
import time
import zipfile
from terminal_probe import Probe


def write_traced_launcher(path,repo):
    path.write_text('''#!/usr/bin/python3
import json,os,sys,time,signal,faulthandler
sys.path.insert(0, ROOT)
from agi import store
from agi.ui import Screen
store._prepare()
trace=open(os.path.join(os.environ['AGI_DATA'],'input-trace.jsonl'),'w',buffering=1)
def record(event,**fields):trace.write(json.dumps(dict(event=event,at=time.monotonic(),**fields))+'\\n')
stack=open(os.path.join(os.environ['AGI_DATA'],'stacks.txt'),'w')
faulthandler.register(signal.SIGUSR1,file=stack,all_threads=True)
original=Screen.handle_pending
def pending(self):
 record('pending',count=len(self.pending),tail=repr(self.pending[-12:]))
 result=original(self)
 record('handled',draft_len=len(self.draft),cursor=self.cursor,chips=len(self.chips),remaining=len(self.pending),pasting=None if self.pasting is None else len(self.pasting))
 return result
Screen.handle_pending=pending
append=store.append_message
def timed(*args,**kwargs):
 started=time.monotonic()
 try:
  result=append(*args,**kwargs)
  record('append_ok',elapsed=time.monotonic()-started,id=result['id'])
  return result
 except BaseException as error:
  record('append_error',elapsed=time.monotonic()-started,error=repr(error))
  raise
store.append_message=timed
atomic=store._atomic_write
def timed_atomic(path,data):
 started=time.monotonic()
 try:return atomic(path,data)
 finally:record('atomic',file=str(path.relative_to(store.DATA_DIR)),elapsed=time.monotonic()-started)
store._atomic_write=timed_atomic
from agi.cli import main
main()
'''.replace('ROOT',repr(str(repo))))
    path.chmod(0o700)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('launcher',type=Path)
    parser.add_argument('--rounds',type=int,default=30)
    parser.add_argument('--store-load',action='store_true')
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if args.rounds<1:parser.error('rounds must be positive')
    launcher=args.launcher.resolve();repo=launcher.parent.parent
    Terminal=runpy.run_path(str(Path(__file__).with_name('test_ui.py')))['Terminal']
    payload='\n'.join(f'line {n} '+'x'*60 for n in range(200))
    results=[];load=None;load_codes=[]
    with tempfile.TemporaryDirectory(prefix='agi-paste-stress-',dir=os.environ.get('AGI_SCRATCH')) as directory:
        root=Path(directory);wrapper=root/'traced-chat'
        write_traced_launcher(wrapper,repo)
        with (root/'store-load.txt').open('w+') as load_output:
            try:
                for iteration in range(args.rounds):
                    if args.store_load and (load is None or load.poll() is not None):
                        if load is not None:load_codes.append(load.returncode)
                        load=subprocess.Popen(['python3',str(repo/'tests/test_store.py'),'-q'],cwd=repo,stdout=load_output,stderr=load_output,env=dict(os.environ,PYTHONPATH=str(repo)))
                    data=root/f'case-{iteration}';data.mkdir();probe=Probe(wrapper,data)
                    terminal=Terminal(24,80);decoder=codecs.getincrementaldecoder('utf-8')('replace');offset=0
                    def view():
                        nonlocal offset
                        terminal.feed(decoder.decode(bytes(probe.output[offset:])));offset=len(probe.output)
                        return terminal.text()
                    writes=[]
                    def send(raw):
                        count=os.write(probe.master,raw);writes.append(dict(expected=len(raw),written=count));probe.collect(.05)
                    start=time.monotonic()
                    try:
                        send(b'\x1b[200~'+payload.encode()+b'\x1b[201~')
                        chip=probe.until(lambda:'[Pasted text #1 · 200 lines]' in view(),3)
                        send(b'a');chip_time=time.monotonic()-start
                        sent_at=time.monotonic();send(b'\x7f\r')
                        messages=probe.wait_messages(1,5)
                        def completed():
                            events=[json.loads(line) for line in (data/'input-trace.jsonl').read_text().splitlines()]
                            appended=False
                            for event in events:
                                appended |= event['event']=='append_ok'
                                if appended and event['event']=='handled' and event['draft_len']==0:return True
                            return False
                        complete=probe.until(completed,5)
                        outcome=dict(iteration=iteration,chip_visible=chip,chip_seconds=chip_time,send_seconds=time.monotonic()-sent_at,writes=writes,
                                     ui_completed=complete,correct=len(messages)==1 and messages[0]['text']==payload,exit=probe.process.poll(),
                                     events=[json.loads(line) for line in (data/'input-trace.jsonl').read_text().splitlines()])
                        if not complete or not outcome['correct'] or not chip or chip_time>=3 or any(w['expected']!=w['written'] for w in writes):
                            outcome['screen']=view();outcome['trace']=(data/'input-trace.jsonl').read_text()
                            probe.collect(10);outcome['late_messages']=probe.messages();outcome['late_screen']=view()
                            probe.stop()
                            with zipfile.ZipFile(args.report.with_suffix('.zip'),'w',zipfile.ZIP_DEFLATED) as archive:
                                for path in data.rglob('*'):
                                    if path.is_file():archive.write(path,path.relative_to(data))
                                archive.writestr('terminal.raw',bytes(probe.output))
                            results.append(outcome);break
                        results.append(outcome)
                    finally:probe.close()
            finally:
                if load is not None:
                    load.wait(timeout=30);load_codes.append(load.returncode)
            load_output.seek(0);load_text=load_output.read()
    report=dict(rounds_requested=args.rounds,rounds_completed=len(results),store_load=args.store_load,load_exit_codes=load_codes,load_output=load_text,
                median_observed_send_ms=round(statistics.median(r['send_seconds'] for r in results)*1000,3),max_observed_send_ms=round(max(r['send_seconds'] for r in results)*1000,3),results=results)
    args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('results','load_output')},indent=2))
    if any(not r['ui_completed'] or not r['correct'] or not r['chip_visible'] or r['chip_seconds']>=3 for r in results) or any(load_codes):raise SystemExit(1)


if __name__=='__main__':main()
