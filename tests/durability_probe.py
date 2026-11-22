"""Interrupt actual UI sends inside append; isolated Storage fixtures, never live data.

Run: python3 tests/durability_probe.py bin/agi. Process exits simulate software interruption,
not loss of USB power or filesystem caches. Each failed child exits with code 86.
"""
import argparse
import json
from pathlib import Path
import os
import tempfile

from terminal_probe import Probe


INJECTOR = '''#!/usr/bin/python3
import os, sys
sys.path.insert(0, ROOT)
from agi import store
stage = STAGE
original_write, original_sync = os.write, os.fsync
original_directory, original_atomic = store._sync_directory, store._atomic_write
written = False

def is_log(fd):
    try:
        return os.readlink(f'/proc/self/fd/{fd}').endswith('.jsonl')
    except OSError:
        return False

def write(fd, data):
    global written
    if is_log(fd):
        if stage == 'before_write':
            os._exit(86)
        if stage == 'partial_write':
            original_write(fd, data[:len(data)//2])
            os._exit(86)
        result = original_write(fd, data)
        written = True
        if stage == 'before_fsync':
            os._exit(86)
        return result
    return original_write(fd, data)

def sync(fd):
    result = original_sync(fd)
    if is_log(fd) and stage == 'after_fsync':
        os._exit(86)
    return result

def directory(path):
    result = original_directory(path)
    if written and path == store.CHAT_DIR and stage == 'after_directory':
        os._exit(86)
    return result

def atomic(path, data):
    if written and path == store.DATA_DIR / 'seq' and stage == 'before_sequence':
        os._exit(86)
    result = original_atomic(path, data)
    if written and path == store.DATA_DIR / 'seq' and stage == 'after_sequence':
        os._exit(86)
    return result

os.write, os.fsync = write, sync
store._sync_directory, store._atomic_write = directory, atomic
from agi.cli import main
main()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('launcher', type=Path)
    args = parser.parse_args()
    launcher = args.launcher.resolve()
    results = {}
    stages = ('before_write', 'partial_write', 'before_fsync', 'after_fsync',
              'after_directory', 'before_sequence', 'after_sequence')
    for stage in stages:
        with tempfile.TemporaryDirectory(prefix='agi-durability-',
                dir=os.environ.get('AGI_SCRATCH')) as directory:
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            wrapper = root / 'crash-chat'
            wrapper.write_text(INJECTOR.replace('ROOT', repr(str(launcher.parent.parent)))
                               .replace('STAGE', repr(stage)))
            wrapper.chmod(0o700)
            probe = Probe(wrapper, data)
            try:
                probe.send(b'durable intent\r')
                probe.process.wait(timeout=4)
                reached = probe.process.returncode == 86
            finally:
                probe.close()
            interrupted = b''.join(path.read_bytes() for path in sorted((data / 'chat').glob('*.jsonl')))
            probe = Probe(launcher, data)
            try:
                uncommitted = stage in ('before_write', 'partial_write')
                restored = b'Restored your unsent draft' in probe.output
                probe.send(b'\r' if uncommitted else b'fresh intent\r')
                records = probe.messages()
                expected = ['durable intent'] if uncommitted else ['durable intent', 'fresh intent']
                tokens = [record.get('request_id') for record in records]
                partials = list((data / 'recovery').glob('*.partial'))
                results[stage] = {
                    'injection_reached': reached,
                    'correct_draft_recovery': restored == uncommitted,
                    'exactly_once': [record['text'] for record in records] == expected,
                    'distinct_request_tokens': None not in tokens and len(set(tokens)) == len(tokens),
                    'recovery_artifacts_correct': bool(partials) == (stage == 'partial_write'),
                    'partial_bytes_preserved': (len(partials) == 1 and partials[0].read_bytes() == interrupted
                                                if stage == 'partial_write' else True),
                }
            finally:
                probe.close()
    print(json.dumps(results, indent=2))
    if not all(all(checks.values()) for checks in results.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
