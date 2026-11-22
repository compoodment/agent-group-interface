"""Independent black-box terminal checks; all writes stay in disposable test data."""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time


# This probe checks terminal behavior. The integration latency test measures speed.
STARTUP_TIMEOUT = 12
SCRATCH = os.environ.get('AGI_SCRATCH')


class Probe:
    def __init__(self, launcher, data_directory=None):
        self.directory=(tempfile.TemporaryDirectory(prefix='agi-pty-',dir=SCRATCH)
                        if data_directory is None else None)
        self.root=Path(self.directory.name if self.directory else data_directory)
        self.master,slave=pty.openpty()
        self.original_lflag=termios.tcgetattr(slave)[3]
        fcntl.ioctl(slave,0x5414,struct.pack('HHHH',24,80,0,0))
        env=dict(os.environ,TERM='xterm-256color',AGI_DATA=str(self.root),AGI_NOTIFY='0',AGI_SETUP_NOTICE='0')
        self.process=subprocess.Popen([str(launcher)],stdin=slave,stdout=slave,stderr=slave,env=env,start_new_session=True)
        os.close(slave)
        self.output=bytearray()
        deadline=time.monotonic()+STARTUP_TIMEOUT
        while '❯'.encode() not in self.output and self.process.poll() is None and time.monotonic()<deadline:
            self.collect(.05)
        if '❯'.encode() not in self.output:
            exit_code=self.process.poll()
            reason=(f'exited with status {exit_code}' if exit_code is not None
                    else f'no prompt after {STARTUP_TIMEOUT} seconds')
            if exit_code is not None:
                self.collect(.05)
            details=self.output.decode(errors='replace')
            self.close()
            raise RuntimeError(f'Chat prompt did not become ready ({reason}): {details}')

    def collect(self,duration=.25):
        if self.master is None:return
        deadline=time.monotonic()+duration
        while time.monotonic()<deadline:
            ready,_,_=select.select([self.master],[],[],max(0,deadline-time.monotonic()))
            if not ready:break
            try:data=os.read(self.master,65536)
            except OSError as error:
                if error.errno==errno.EIO:break
                raise
            if not data:break
            self.output.extend(data)

    def send(self,data):
        os.write(self.master,data)
        self.collect()

    def until(self,predicate,timeout=3):
        """Wait for an observed behavior, without treating a slow machine as a failure."""
        deadline=time.monotonic()+timeout
        while not predicate():
            if self.process.poll() is not None or time.monotonic()>=deadline:
                return False
            self.collect(min(.02,max(0,deadline-time.monotonic())))
        return True

    def wait_messages(self,count,timeout=3):
        self.until(lambda:len(self.messages())>=count,timeout)
        return self.messages()

    def messages(self):
        result=[]
        for path in sorted((self.root/'chat').glob('*.jsonl')):
            for line in path.read_text().splitlines():
                try:result.append(json.loads(line))
                except ValueError:pass
        return result

    def resize(self,rows,columns):
        fcntl.ioctl(self.master,0x5414,struct.pack('HHHH',rows,columns,0,0))
        self.process.send_signal(signal.SIGWINCH)
        self.collect()

    def stop(self,sig=signal.SIGINT):
        if self.process.poll() is None:
            self.process.send_signal(sig)
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
        self.collect(.1)

    def close(self):
        self.stop()
        if self.master is not None:os.close(self.master)
        if self.directory:self.directory.cleanup()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('launcher',type=Path)
    args=parser.parse_args()
    launcher=args.launcher.resolve()
    results={}
    p=Probe(launcher)
    try:
        p.send(b'\x1b[200~first line\nsecond line\x1b[201~')
        results['paste_stays_draft']=not p.messages()
        p.send(b'\r')
        messages=p.wait_messages(1)
        results['paste_sends_one_message']=len(messages)==1 and messages[0]['text']=='first line\nsecond line'
    finally:p.close()
    p=Probe(launcher)
    try:
        p.send(b'\x1b[20')
        p.send(b'0~first line\rsecond line\x1b[201~')
        results['fragmented_start_stays_draft']=not p.messages()
        p.send(b'\r')
        messages=p.wait_messages(1)
        results['fragmented_start_sends_one_message']=len(messages)==1 and messages[0]['text']=='first line\nsecond line'
    finally:p.close()
    p=Probe(launcher)
    try:
        text='    indented line\nsecond line\n'
        p.send(b'\x1b[200~'+text.encode()+b'\x1b[201~')
        p.send(b'\r')
        messages=p.wait_messages(1)
        results['paste_preserves_indentation']=len(messages)==1 and messages[0]['text']==text.rstrip()
    finally:p.close()
    p=Probe(launcher)
    try:
        broken=p.root/'chat/2000-01-01.jsonl'
        broken.write_text('invalid committed record\n')
        p.send(b'hello\r')
        results['failed_send_error_visible']=p.until(lambda:b'Not sent:' in p.output)
        broken.unlink()
        p.send(b'\x7f\r')
        messages=p.wait_messages(1)
        results['failed_send_keeps_editable_draft']=len(messages)==1 and messages[0]['text']=='hell'
    finally:p.close()
    p=Probe(launcher)
    try:
        p.resize(3,10)
        p.resize(24,80)
        p.send(b'after resize\r')
        results['interactive_after_small_resize']=any(m['text']=='after resize' for m in p.wait_messages(1))
        results['no_traceback_on_resize']=b'Traceback' not in p.output
        p.stop(signal.SIGTERM)
        results['sigterm_restores_alt_screen']=p.output.rfind(b'\x1b[?1049l') > p.output.rfind(b'\x1b[?1049h')
        mask=termios.ECHO|termios.ICANON|termios.ISIG
        results['sigterm_restores_terminal_flags']=(termios.tcgetattr(p.master)[3]&mask)==(p.original_lflag&mask)
        results['sigterm_exit_code']=p.process.returncode
        results['paste_mode_enabled']=b'\x1b[?2004h' in p.output
        results['sigterm_restores_paste_mode']=b'\x1b[?2004l' in p.output if results['paste_mode_enabled'] else False
    finally:p.close()
    p=Probe(launcher)
    try:
        os.close(p.master)
        p.master=None
        try:
            p.process.wait(timeout=STARTUP_TIMEOUT)  # it exits within a second; a busy host needed more than 2 s
            results['terminal_hangup_exits_promptly']=True
        except subprocess.TimeoutExpired:
            results['terminal_hangup_exits_promptly']=False
    finally:p.close()
    print(json.dumps(results,indent=2))
    if any(value is False for value in results.values()):
        raise SystemExit(1)


if __name__=='__main__':main()
