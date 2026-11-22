"""Observed UI readiness, verified key echo and idle resources; synthetic data only.

Run: python3 tests/benchmark_ui.py bin/agi --history 5000 --idle-seconds 10.
Includes PTY/test-emulator overhead; not a direct renderer timing or a cold-disk benchmark.
"""
import argparse
import codecs
import json
import os
from pathlib import Path
import runpy
import select
import statistics
import tempfile
import time

from terminal_probe import Probe


def usage(pid):
    stat=Path(f'/proc/{pid}/stat').read_text().split()
    fields={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in
            Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line}
    return int(stat[13])+int(stat[14]),int(fields['VmRSS'].split()[0])/1024,int(fields['VmHWM'].split()[0])/1024


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('launcher',type=Path)
    parser.add_argument('--history',type=int,default=5000)
    parser.add_argument('--idle-seconds',type=float,default=10)
    args=parser.parse_args()
    if args.history<0 or not 1<=args.idle_seconds<=60:parser.error('history >= 0; idle seconds from 1 to 60')
    Terminal=runpy.run_path(str(Path(__file__).with_name('test_ui.py')))['Terminal']
    with tempfile.TemporaryDirectory(prefix='agi-ui-bench-',dir=os.environ.get('AGI_SCRATCH')) as directory:
        root=Path(directory);(root/'chat').mkdir()
        with (root/'chat/2026-11-16.jsonl').open('w') as stream:
            for number in range(1,args.history+1):
                stream.write(json.dumps({'id':number,'ts':'2026-11-16T11:00:00+01:00','sender':'user',
                                         'kind':'msg','text':f'History {number}: café 日本語 🙂 **bold** and `code`.'})+'\n')
        started=time.perf_counter();probe=Probe(args.launcher.resolve(),root)
        readiness=(time.perf_counter()-started)*1000
        try:
            terminal=Terminal(24,80);decoder=codecs.getincrementaldecoder('utf-8')('replace')
            terminal.feed(decoder.decode(bytes(probe.output)))
            delays=[];target=''
            for character in '_bench_echo_12345':
                target+=character;started=time.perf_counter()
                os.write(probe.master,character.encode())
                end=time.monotonic()+3
                while target not in terminal.text() and time.monotonic()<end:
                    if not select.select([probe.master],[],[],.05)[0]:continue
                    data=os.read(probe.master,65536);probe.output.extend(data)
                    terminal.feed(decoder.decode(data))
                if target not in terminal.text():raise RuntimeError('Verified echo timed out')
                delays.append((time.perf_counter()-started)*1000)
            probe.send(b'\x15');probe.collect(1.2)     # clear the draft and settle its autosave
            before,rss,peak=usage(probe.process.pid);started=time.monotonic()
            probe.collect(args.idle_seconds)
            after,final_rss,final_peak=usage(probe.process.pid)
            cpu=(after-before)/os.sysconf('SC_CLK_TCK')/(time.monotonic()-started)*100
            print(json.dumps({'history_records':args.history,'observed_prompt_ready_ms':round(readiness,3),
                              'verified_echo_median_ms':round(statistics.median(delays),3),
                              'verified_echo_max_ms':round(max(delays),3),'first_echo_ms':round(delays[0],3),
                              'verified_echo_samples_ms':[round(value,3) for value in delays],'idle_cpu_percent':round(cpu,3),
                              'resident_mib':round(final_rss,3),'peak_resident_mib':round(max(peak,final_peak),3)},indent=2))
        finally:probe.close()


if __name__=='__main__':main()
