"""Replay the exact UI chip unittest under store load, saving pre-teardown failure evidence."""
import argparse
import io
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import tempfile
import time
import unittest
import zipfile
from paste_stress import write_traced_launcher


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('launcher',type=Path)
    parser.add_argument('--rounds',type=int,default=60)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--minimal-trace',action='store_true')
    parser.add_argument('--unmodified',action='store_true')
    parser.add_argument('--pristine',action='store_true')
    args=parser.parse_args()
    if args.rounds<1:parser.error('rounds must be positive')
    repo=args.launcher.resolve().parent.parent
    namespace=runpy.run_path(str(repo/'tests/test_ui.py'))
    case_type=namespace['ChatScreenTests'];screen_type=namespace['ChatScreen'];base=namespace['ScreenCase']
    original_init=screen_type.__init__
    def initialize(self,*a,**kw):
        original_init(self,*a,**kw)
        if '❯' not in self.term.text():raise RuntimeError('UI startup failed: '+self.raw.decode(errors='replace'))
    screen_type.__init__=initialize
    original_type=screen_type.type
    inputs=[]
    def type_input(self,data,settle=.05):
        raw=data.encode() if isinstance(data,str) else data
        count=os.write(self.fd,raw)
        inputs.append(dict(at=time.monotonic(),expected=len(raw),written=count,tail=repr(raw[-12:])))
        self.pump(settle)
    screen_type.type=type_input
    if args.pristine:
        args.unmodified=True
        screen_type.type=original_type
        screen_type.__init__=original_init
    original_until=screen_type.until
    def until(self,condition,seconds=5):return original_until(self,condition,seconds)
    screen_type.until=until
    original_teardown=base.tearDown;capture={}
    def teardown(self):
        outcome=getattr(self,'_outcome',None);result=getattr(outcome,'result',None)
        failed=result and any(test is self for test,_ in result.failures+result.errors)
        if not failed:
            original_teardown(self)
            return
        capture.clear()
        try:
            if self.screen:
                try:
                    if not args.unmodified:os.kill(self.screen.pid,signal.SIGUSR1)
                    if not args.unmodified:self.screen.pump(.05)
                except ProcessLookupError:pass
                files={}
                for path in Path(self.folder.name).rglob('*'):
                    if path.is_file():
                        try:files[str(path.relative_to(self.folder.name))]=path.read_bytes()
                        except FileNotFoundError:pass  # atomic-save temporary file renamed during snapshot
                capture.update(screen=self.screen.term.text(),raw=self.screen.raw,files=files)
                threads={}
                for path in Path(f'/proc/{self.screen.pid}/task').glob('*/wchan'):
                    try:threads[path.parent.name]=path.read_text()
                    except FileNotFoundError:pass
                capture['threads']=threads
        finally:
            original_teardown(self)
    base.tearDown=teardown
    results=[];load=None;codes=[]
    with tempfile.TemporaryDirectory(prefix='agi-exact-paste-',dir=os.environ.get('AGI_SCRATCH')) as directory:
        root=Path(directory);wrapper=root/'traced-chat';write_traced_launcher(wrapper,repo)
        if args.minimal_trace:
            wrapper.write_text('#!/usr/bin/python3\nimport sys,os,signal,faulthandler\nsys.path.insert(0,'+repr(str(repo))+')\nfrom agi import store\nstore._prepare()\nstack=open(os.path.join(os.environ["AGI_DATA"],"stacks.txt"),"w")\nfaulthandler.register(signal.SIGUSR1,file=stack,all_threads=True)\nfrom agi.cli import main\nmain()\n')
        original_init.__globals__['LAUNCHER']=str(args.launcher.resolve() if args.unmodified else wrapper)
        with (root/'load.txt').open('w+') as loadout:
            try:
                for iteration in range(args.rounds):
                    if load is None or load.poll() is not None:
                        if load is not None:codes.append(load.returncode)
                        load=subprocess.Popen(['python3',str(repo/'tests/test_store.py'),'-q'],cwd=repo,
                              env=dict(os.environ,PYTHONPATH=str(repo)),stdout=loadout,stderr=loadout)
                    inputs.clear();output=io.StringIO();started=time.monotonic()
                    result=unittest.TextTestRunner(stream=output).run(case_type('test_large_paste_becomes_a_chip_and_sends_in_full'))
                    row=dict(iteration=iteration,seconds=time.monotonic()-started,success=result.wasSuccessful(),output=output.getvalue())
                    results.append(row)
                    if not result.wasSuccessful():
                        row['screen']=capture.get('screen');row['threads']=capture.get('threads');row['inputs']=list(inputs)
                        with zipfile.ZipFile(args.report.with_suffix('.zip'),'w',zipfile.ZIP_DEFLATED) as archive:
                            for name,data in capture.get('files',{}).items():archive.writestr(name,data)
                            archive.writestr('terminal.raw',capture.get('raw',b''))
                            archive.writestr('screen.txt',capture.get('screen',''))
                        break
            finally:
                if load is not None:load.wait(timeout=30);codes.append(load.returncode)
            loadout.seek(0);load_text=loadout.read()
    args.report.write_text(json.dumps(dict(results=results,load_codes=codes,load_output=load_text),indent=2)+'\n')
    print(json.dumps(dict(rounds=len(results),load_codes=codes,failures=[r for r in results if not r['success']]),indent=2))
    if any(not r['success'] for r in results) or any(codes):raise SystemExit(1)


if __name__=='__main__':main()
