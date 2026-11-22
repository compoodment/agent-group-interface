"""Compare cold load and warm UI reads; synthetic logs only, no live chat writes."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import statistics
import tempfile
import time


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(path, name, count):
    module = load(path, name)
    started = time.perf_counter()
    messages = module.read_messages()
    cold = time.perf_counter() - started
    samples = []
    for _ in range(7):
        started = time.perf_counter()
        module.read_messages(count)
        module.guard_status()
        module.active_claims()
        samples.append(time.perf_counter() - started)
    assert len(messages) == count
    tail_module = load(path, name + '_tail')
    started = time.perf_counter()
    tail = tail_module.read_tail(min(1000, count))
    tail_ms = (time.perf_counter() - started) * 1000
    assert len(tail) == min(1000, count) and tail[-1]['id'] == count
    return {'cold_tail_ms': round(tail_ms, 3), 'cold_ms': round(cold * 1000, 3),
            'warm_refresh_median_ms': round(statistics.median(samples) * 1000, 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--autonomous', action='store_true', help='Use mixed agent history and autonomous mode')
    args = parser.parse_args()
    candidate = Path(__file__).resolve().parents[1]/'agi/store.py'
    results = {}
    for count in (1000, 20000):
        with tempfile.TemporaryDirectory(prefix='agi-bench-', dir=os.environ.get('AGI_SCRATCH')) as directory:
            os.environ['AGI_DATA'] = directory
            chat = Path(directory)/'chat'
            chat.mkdir()
            with (chat/'2026-11-16.jsonl').open('w') as stream:
                for number in range(1, count+1):
                    stream.write(json.dumps({'id':number,'ts':'2026-11-16T10:00:00+01:00',
                                             'sender':('codex' if args.autonomous and number % 3 else 'user'),'kind':'msg','text':'message '+str(number)})+'\n')
            if args.autonomous:
                (Path(directory)/'autonomy.json').write_text(json.dumps({'expires_at':time.time()+3600}))
            results[count] = {'baseline':run(args.baseline, 'baseline', count),
                              'candidate':run(candidate, 'candidate', count)}
    print(json.dumps(results, indent=2))


if __name__ == '__main__':main()
