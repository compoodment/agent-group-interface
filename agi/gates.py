"""Read local integration status from supported repositories."""

from contextlib import suppress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid


GATE_STATUS_HISTORY = 12
GATE_STATUS_RECORD_LIMIT = 64 * 1024
RUN_ID = re.compile(r"[0-9a-f]{32}")
CANDIDATE = re.compile(r"[0-9a-f]{40,64}")
ACTIVE = {
    "waiting": "Starting",
    "rebasing": "Preparing",
    "mini_requested": "Runner requested",
    "checking_local": "Local checks",
    "merging": "Merging",
}
FINISHED = ("integrated", "failed")


def _boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _process_start(pid):
    # Field 22 is the process start tick. The name before it may contain parentheses.
    return int(Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[19])


def _read_status(path):
    if not RUN_ID.fullmatch(path.stem) or not path.is_file():
        return None
    with path.open("rb") as record:
        data = record.read(GATE_STATUS_RECORD_LIMIT + 1)
    if len(data) <= GATE_STATUS_RECORD_LIMIT:
        return json.loads(data)


class GateStatus:
    """Publish a local observation without taking part in the gate's decisions."""

    def __init__(self, common, branch):
        self.directory = common / 'gate-status'
        self.record = None
        self.warned = False
        try:
            run_id = uuid.uuid4().hex
            boot_id = _boot_id()
            process_started = _process_start(os.getpid())
        except (OSError, ValueError, IndexError) as error:
            self.warn(error)
            return
        now = time.time()
        self.path = self.directory / f'{run_id}.json'
        self.record = dict(version=1, run_id=run_id, pid=os.getpid(), boot_id=boot_id,
                           process_started=process_started, state='waiting', branch=branch,
                           candidate=None, started_at=now, updated_at=now)

    def warn(self, error):
        if not self.warned:
            self.warned = True
            with suppress(OSError, ValueError):
                print(f'AGI gate status: {error}', file=sys.stderr)

    def __enter__(self):
        self.update('waiting')
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        if self.record is None:
            return
        if exc_type is not None and self.record['state'] != 'integrated':
            self.update('failed')
        try:
            self.prune()
        except OSError as error:
            self.warn(error)

    def update(self, state, *, candidate=None):
        if self.record is None:
            return
        self.record.update(state=state, updated_at=time.time())
        if candidate is not None:
            self.record['candidate'] = candidate
        temporary = None
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                             prefix='.gate-', delete=False) as output:
                temporary = Path(output.name)
                json.dump(self.record, output)
            os.replace(temporary, self.path)
            temporary = None
        except OSError as error:
            self.warn(error)
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def prune(self):
        completed = []
        for path in self.directory.glob('*.json'):
            try:
                record = _read_status(path)
                if (isinstance(record, dict) and record.get('version') == 1
                        and record.get('run_id') == path.stem
                        and record.get('state') in ('integrated', 'failed')):
                    completed.append((path.stat().st_mtime_ns, path))
            except (FileNotFoundError, ValueError, RecursionError):
                continue
        for _, path in sorted(completed, reverse=True)[GATE_STATUS_HISTORY:]:
            path.unlink(missing_ok=True)


def _live_status(directory, boot_id):
    records = []
    for path in directory.glob("*.json"):
        try:
            record = _read_status(path)
            if (not isinstance(record, dict) or record.get("version") != 1
                    or record.get("run_id") != path.stem or record.get("boot_id") != boot_id
                    or not isinstance(record.get("state"), str) or record["state"] not in ACTIVE):
                continue
            pid = record.get("pid")
            if (type(pid) is not int or pid <= 0
                    or record.get("process_started") != _process_start(pid)
                    or not isinstance(record.get("branch"), str)):
                continue
            records.append(record)
        except (OSError, ValueError, IndexError, RecursionError):
            continue
    return records


def _lock_holder(path, locks):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    for line in locks:
        fields = line.split()
        if fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"] or len(fields) < 6:
            continue
        major, minor, inode = fields[5].split(":")
        if (int(major, 16), int(minor, 16), int(inode)) == (
                os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino):
            return int(fields[4])
    return None


def snapshot(main):
    """Read the actual lock holder and live waiting requests without acquiring any locks."""
    try:
        boot_id = _boot_id()
        locks = Path("/proc/locks").read_text().splitlines()
    except OSError:
        return None
    projects = [("AGI", main / ".git", "agi-integration.lock")]
    artlab = os.environ.get("ARTLAB_GATE_REPO")
    if artlab:
        projects.append(("Artlab", Path(artlab).expanduser() / ".git", "artlab-integration.lock"))
    result = []
    for label, common, lock in projects:
        if not common.is_dir():
            continue
        records = _live_status(common / "gate-status", boot_id)
        try:
            holder = _lock_holder(common / lock, locks)
        except OSError:
            result.append((label, "Status unavailable", (), ()))
            continue
        active = next((record for record in records if record["pid"] == holder), None)
        if active is not None:
            sha = active.get("candidate")
            revision = sha[:7] if isinstance(sha, str) and CANDIDATE.fullmatch(sha) else ""
            detail = (active["branch"], revision)
            stage = ACTIVE[active["state"]]
        else:
            stage = "Lock held" if holder is not None else "Idle"
            detail = ()
        queued = tuple(sorted(record["branch"] for record in records
                              if record["state"] == "waiting" and record["pid"] != holder))
        result.append((label, stage, detail, queued))
    return tuple(result)
