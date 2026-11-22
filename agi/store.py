"""Shared, durable message storage. Owner: Codex.

Chat mutations use one flock; presence and UI state have separate locks.
Complete log records are append-only. After
an interrupted write, an incomplete suffix is preserved separately before the
next append repairs the tail. Metadata is replaced atomically.
"""

from bisect import bisect_left, bisect_right
import ctypes
import fcntl
import hashlib
import json
import math
import os
import re
import select
import stat
import struct
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from agi import png_attachment
from agi import gate_reminders
from agi import release_tags
from agi import todo
from agi import decisions

_state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
if not _state_home.is_absolute():
    _state_home = Path.home() / ".local" / "state"
DATA_DIR = Path(os.environ.get("AGI_DATA") or _state_home / "agi").expanduser()
CHAT_DIR = DATA_DIR / "chat"
CURSOR_DIR = DATA_DIR / "cursors"
CHECKOUT_ROOT = Path(__file__).resolve().parents[1]

USER = "user"
ADMIN = "admin"
AGENTS = ("claude", "codex")
GUARDED_SENDERS = (*AGENTS, ADMIN)
SENDERS = (USER, "system", *GUARDED_SENDERS)
KINDS = ("msg", "claim", "release", "system")
_UNCHANGED_WAIT = object()
GUARD_LIMIT = 4
MAX_HANDOFF_NOTE_CHARS = 500
MAX_TASK_DOING_CHARS = 160
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
ATTACHMENT_PATH = re.compile(r"attachments/[0-9a-f]{64}\.png")
MAX_ATTACHMENTS = 4
TASK_KEY = re.compile(r"task:[1-9][0-9]*")
REMINDER_ID = re.compile(r"[0-9a-f]{32}")
MAX_REMINDER_NOTE = 500
_REPLY_PREFIX = re.compile(r"^↪ #([0-9]{1,12})\s+")
_PROMISE_PREFIX = re.compile(r"^(?:(?:↪ #\d+|@\w+|[-*]|\d+[.)])\s+)+")
_PROMISE = re.compile(
    r"^(?:(?:after|before|once|when|if|on the next)\b[^.?!]{1,160}?[, ]\s*)?"
    r"(?:i['’]ll|we['’]ll|i will|we will|i['’]m going to)\s+\w", re.IGNORECASE,
)
_IMMEDIATE = re.compile(r"\b(?:now|right away|in this turn)\b", re.IGNORECASE)
_QUOTED_PROSE = re.compile(r'"(?:[^"\\]|\\.)*(?:"|$)|“[^”]*(?:”|$)')
_SENTENCE_GAP = re.compile(r"(?<=[.!?])\s+")
_GUARD_NOTICE = "Agent loop guard: "
_RATE_NOTICE = "Agent rate guard: "
_PAUSE_NOTICE = "Agent pause guard: "
RATE_LIMIT = 50
RATE_WINDOW = 600
WAKE_SAMPLE_LIMIT = 20
_cache_lock = threading.RLock()
_file_cache = {}
_record_signature = None
_record_cache = []
_summary_cache = None


class GuardBlocked(Exception):
    """An agent message was refused by the active posting policy."""


class ClaimConflict(Exception):
    """Another sender holds an overlapping claim, or a release is invalid."""


def _prepare():
    for path in (DATA_DIR, CHAT_DIR, CURSOR_DIR):
        # pathlib's exist_ok check can race a directory replacement: mkdir
        # sees the old directory, but is_dir sees the gap before its successor.
        for attempt in range(3):
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                break
            except FileExistsError:
                if attempt == 2:
                    raise


def _file_lock(path, *, exclusive=True, nonblocking=False):
    """Return an owned lock handle; closing it releases the shared/exclusive flock."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    stream = os.fdopen(fd, "r+b")
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, mode | (fcntl.LOCK_NB if nonblocking else 0))
        return stream
    except BaseException:
        stream.close()
        raise


@contextmanager
def _locked(exclusive=False):
    _prepare()
    with _file_lock(DATA_DIR / "lock", exclusive=exclusive):
        yield


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _request_id(value):
    if not isinstance(value, str) or REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id must be 32 lowercase hexadecimal characters")
    return value


def _attachment_ref(ref):
    if not isinstance(ref, dict) or set(ref) != {"path", "width", "height"}:
        raise ValueError("Invalid image attachment reference")
    path = ref["path"]
    if not isinstance(path, str) or ATTACHMENT_PATH.fullmatch(path) is None:
        raise ValueError("Invalid image attachment path")
    png_attachment.check_size(ref["width"], ref["height"])
    return dict(ref)


def _attachments(refs):
    if refs is None:
        return []
    if not isinstance(refs, (list, tuple)) or len(refs) > MAX_ATTACHMENTS:
        raise ValueError(f"A message can have up to {MAX_ATTACHMENTS} image attachments")
    return [_attachment_ref(ref) for ref in refs]


def attachment_path(ref):
    """Return the absolute local path for a stored attachment reference."""
    return DATA_DIR.absolute() / _attachment_ref(ref)["path"]


def attach(source):
    """Validate a PNG and durably store it by content hash."""
    data = png_attachment.read(source)
    width, height = png_attachment.dimensions(data)
    digest = hashlib.sha256(data).hexdigest()
    ref = {"path": f"attachments/{digest}.png", "width": width, "height": height}
    path = attachment_path(ref)
    with _locked(exclusive=True):
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("Image attachment storage must use regular files")
        if path.exists():
            _check_attachment_file(ref)
            _sync_directory(path.parent)
        else:
            _atomic_write(path, data)
        _sync_directory(DATA_DIR)
    return ref


def _check_attachment_file(ref):
    path = attachment_path(ref)
    if path.parent.is_symlink():
        raise ValueError("Image attachment storage must use a local directory")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > png_attachment.MAX_BYTES:
        raise ValueError("Invalid stored image attachment file")
    with path.open("rb") as stream:
        width, height = png_attachment.header(stream.read(33))
    if (width, height) != (ref["width"], ref["height"]):
        raise ValueError("Stored image dimensions differ from its attachment reference")


def _copy_message(message):
    copied = dict(message)
    if "attachments" in copied:
        copied["attachments"] = [dict(ref) for ref in message["attachments"]]
    if "task_reminder" in copied:
        copied["task_reminder"] = dict(message["task_reminder"])
    if "task_reminder_ids" in copied:
        copied["task_reminder_ids"] = list(message["task_reminder_ids"])
    if "todo_event" in copied:
        event = copied["todo_event"]
        copied["todo_event"] = dict(event, changes=dict(event["changes"]))
    if "todo_check" in copied:
        copied["todo_check"] = dict(message["todo_check"])
    if "todo_answer_targets" in copied:
        copied["todo_answer_targets"] = [dict(target) for target in message["todo_answer_targets"]]
    if "todo_answer" in copied:
        copied["todo_answer"] = dict(message["todo_answer"])
    if "task_helper" in copied:
        copied["task_helper"] = dict(message["task_helper"])
    if "task_revocation" in copied:
        copied["task_revocation"] = dict(message["task_revocation"])
    if "todo_next" in copied:
        copied["todo_next"] = dict(message["todo_next"])
    if "deferred_todos" in copied:
        copied["deferred_todos"] = list(message["deferred_todos"])
    if "decision_prompt" in copied:
        prompt = copied["decision_prompt"]
        copied["decision_prompt"] = dict(prompt, choices=list(prompt["choices"]))
    return copied


def delivery_message(message, *, compact=False):
    """Present the record with local absolute paths for the receiving participant."""
    images = [dict(ref, path=str(attachment_path(ref))) for ref in message.get("attachments", ())]
    if compact:
        row = [message[key] for key in ("id", "sender", "kind", "text")]
        if images:
            row.append(images)
        return row
    return dict(message, attachments=images) if images else message


def _records():
    """Read with a lock held. Ignore only an unterminated final record."""
    with _cache_lock:
        return _cached_records()


def _cached_records():
    global _record_signature, _record_cache
    paths = sorted(CHAT_DIR.glob("*.jsonl"))
    signature = tuple((str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                       st.st_ctime_ns) for path in paths for st in [path.stat()])
    if signature == _record_signature:
        return _record_cache
    messages = []
    seen, requests = set(), set()
    next_cache = {}
    for path, stamp in zip(paths, signature):
        previous = _file_cache.get(str(path))
        # App writes only append. A changed inode, truncation or same-size edit
        # requires a full reread; an incomplete line is retried from its start.
        incremental = (previous is not None and previous[0][1:3] == stamp[1:3]
                       and stamp[3] > previous[0][3])
        if previous is not None and previous[0] == stamp:
            records, offset, line_number = previous[1:]
        else:
            records = list(previous[1]) if incremental else []
            offset = previous[2] if incremental else 0
            line_number = previous[3] if incremental else 0
            records, offset, line_number = _read_file(path, records, offset, line_number)
        next_cache[str(path)] = (stamp, records, offset, line_number)
        for message in records:
            if message["id"] in seen:
                raise ValueError(f"Invalid log record at {path.name}: duplicate message id")
            seen.add(message["id"])
            request = message.get("request_id")
            if request is not None:
                if request in requests:
                    raise ValueError(f"Invalid log record at {path.name}: duplicate request id")
                requests.add(request)
        messages.extend(records)
    messages.sort(key=lambda message: message["id"])
    _file_cache.clear()
    _file_cache.update(next_cache)
    _record_signature, _record_cache = signature, messages
    return messages


def _read_file(path, messages, offset, line_number):
    with path.open("rb") as stream:
        stream.seek(offset)
        for line in stream:
            if not line.endswith(b"\n"):
                break
            line_number += 1
            messages.append(_parse_record(line, f"{path.name}:{line_number}"))
            offset += len(line)
    return messages, offset, line_number


def _finite_nonnegative_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _parse_record(line, location):
    try:
        message = json.loads(line)
        if (not isinstance(message, dict) or type(message.get("id")) is not int
                or message["id"] < 1 or message.get("sender") not in SENDERS
                or message.get("kind") not in KINDS
                or not isinstance(message.get("ts"), str)
                or not isinstance(message.get("text"), str)):
            raise ValueError("invalid message fields")
        if message["sender"] == ADMIN and (
                message["kind"] != "msg" or not message["text"].strip() or "attachments" in message):
            raise ValueError("Admin posts require a text-only message")
        if "reply_to" in message:
            target_id = message["reply_to"]
            if (message["kind"] != "msg" or message["sender"] == "system"
                    or type(target_id) is not int or not 0 < target_id < message["id"]
                    or target_id != _reply_id(message["text"])):
                raise ValueError("Invalid reply target")
        if "request_id" in message:
            _request_id(message["request_id"])
            if message["kind"] != "msg" or message["sender"] == "system":
                raise ValueError("request_id is only valid for participant messages")
        if "attachments" in message:
            if message["kind"] != "msg" or message["sender"] == "system" or not _attachments(message["attachments"]):
                raise ValueError("Attachments require a participant message")
        if "done" in message:
            if (message.get("done") is not True or message["kind"] != "release"
                    or message["sender"] not in AGENTS or not TASK_KEY.fullmatch(message["text"])):
                raise ValueError("Completion requires an agent task release")
            if "result" in message and (not isinstance(message["result"], str) or not message["result"].strip()):
                raise ValueError("Task result must contain text")
        if "check" in message or "skills" in message:
            if (message["kind"] != "release" or message["sender"] not in AGENTS
                    or not TASK_KEY.fullmatch(message["text"])):
                raise ValueError("Evidence requires an agent task release")
            _task_evidence(message.get("result"), message.get("check"), message.get("skills"))
        if "note" in message:
            if (message["kind"] != "release" or message["sender"] not in AGENTS
                    or not TASK_KEY.fullmatch(message["text"]) or "done" in message
                    or not isinstance(message["note"], str) or not message["note"].strip()
                    or len(message["note"]) > MAX_HANDOFF_NOTE_CHARS):
                raise ValueError("Handoff note requires an agent task release and must contain text")
        if "doing" in message:
            if (message["kind"] != "claim" or message["sender"] not in AGENTS
                    or not TASK_KEY.fullmatch(message["text"])):
                raise ValueError("Current action requires an agent task claim")
            _task_doing(message["doing"])
        if "doing_update" in message and (message["doing_update"] is not True or "doing" not in message):
            raise ValueError("An action update requires current action text")
        if "task_revocation" in message:
            event = message["task_revocation"]
            if (message["sender"] != "system" or message["kind"] != "system"
                    or not isinstance(event, dict) or set(event) != {"task_id", "claim_id", "owner"}
                    or event["owner"] not in AGENTS
                    or type(event["task_id"]) is not int or type(event["claim_id"]) is not int
                    or not 0 < event["task_id"] < event["claim_id"] < message["id"]):
                raise ValueError("Invalid user task claim release")
        if "handoff_to" in message:
            target = message["handoff_to"]
            if (message["kind"] != "release" or message["sender"] not in AGENTS
                    or not TASK_KEY.fullmatch(message["text"]) or "done" in message
                    or target not in AGENTS or target == message["sender"]):
                raise ValueError("Named handoff requires an open task release to the other agent")
        if "deferred_todos" in message:
            items = message["deferred_todos"]
            if (message["kind"] != "release" or message["sender"] not in AGENTS
                    or not TASK_KEY.fullmatch(message["text"]) or "done" in message or "handoff_to" in message
                    or not isinstance(items, list) or not items
                    or any(type(item) is not int or not 0 < item < message["id"] for item in items)
                    or len(set(items)) != len(items)):
                raise ValueError("Deferral requires an open task release linked to distinct earlier to-dos")
        if "task_reminder" in message:
            event = message["task_reminder"]
            if (message["sender"] != "system" or message["kind"] != "system"
                    or not isinstance(event, dict)
                    or not isinstance(event.get("id"), str)
                    or REMINDER_ID.fullmatch(event["id"]) is None
                    or type(event.get("task_id")) is not int or event["task_id"] < 1
                    or event.get("state") not in ("scheduled", "cancelled")):
                raise ValueError("Invalid task reminder event")
            trigger = event.get("trigger", "time")  # old timer records remain valid
            if trigger not in ("time", "gate"):
                raise ValueError("Invalid task reminder trigger")
            common = {"id", "task_id", "state", "note", "trigger"}
            if trigger == "time":
                allowed = common | {"due_at"}
            else:
                allowed = common | {"project", "commit"}
                try:
                    gate_reminders.validate_target_format(event.get("project"), event.get("commit"))
                except ValueError as error:
                    raise ValueError("Invalid gate task reminder target") from error
            if set(event) - allowed:
                raise ValueError("Invalid task reminder fields")
            if event["state"] == "scheduled":
                if trigger == "time" and not _finite_nonnegative_number(event.get("due_at")):
                    raise ValueError("Invalid scheduled task reminder")
                if trigger == "gate" and ("due_at" in event or not event.get("project") or not event.get("commit")):
                    raise ValueError("Invalid scheduled gate reminder")
                if "note" in event and (not isinstance(event["note"], str) or not event["note"].strip()
                                         or len(event["note"]) > MAX_REMINDER_NOTE):
                    raise ValueError("Invalid task reminder note")
            else:
                if trigger == "time" and "due_at" in event and not _finite_nonnegative_number(event["due_at"]):
                    raise ValueError("Invalid cancelled task reminder time")
                if trigger == "gate" and ("due_at" in event or not event.get("project") or not event.get("commit")):
                    raise ValueError("Invalid cancelled gate reminder")
                if "note" in event and (not isinstance(event["note"], str) or not event["note"].strip()
                                         or len(event["note"]) > MAX_REMINDER_NOTE):
                    raise ValueError("Invalid cancelled task reminder note")
        if "task_reminder_ids" in message:
            ids = message["task_reminder_ids"]
            if (message["sender"] != "system" or message["kind"] != "msg"
                    or not isinstance(ids, list) or not ids
                    or any(not isinstance(value, str) or REMINDER_ID.fullmatch(value) is None for value in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError("Invalid task reminder delivery")
        if "todo_event" in message:
            event = message["todo_event"]
            todo.validate_event(event)
            if message["kind"] != "system" or message["sender"] != "system":
                raise ValueError("To-do events require a system record")
            if event["id"] is None:
                source_id = event["changes"]["source_id"]
                if source_id is not None and source_id >= message["id"]:
                    raise ValueError("To-do source must precede its record")
            elif not event["id"] <= event["revision"] < message["id"]:
                raise ValueError("To-do revision must precede its record")
        if "todo_wait" in message:
            event = message.get("todo_event")
            if event is None or "result" in event["changes"]:
                raise ValueError("A to-do prerequisite belongs to an open to-do change")
            if message["todo_wait"] is not None:
                todo.wait_target(message["todo_wait"])
                if event["changes"].get("next_actor", True) is not None:
                    raise ValueError("A waiting to-do cannot have a next actor")
        if "todo_ready" in message:
            event = message.get("todo_event")
            if (message["todo_ready"] is not True or event is None or event["id"] is None
                    or "todo_wait" in message or event["changes"] != {"next_actor": event["actor"]}):
                raise ValueError("Invalid to-do readiness record")
            if not message["text"].startswith(f"@{event['actor']} T{event['id']} is ready.\n"):
                raise ValueError("To-do readiness requires a notice to its owner")
        if "todo_check" in message:
            check = message["todo_check"]
            todo.validate_check(check, message["id"])
            recipient = check.get("recipient", check["owner"])
            if (message["sender"] != "system" or message["kind"] != "msg"
                    or "task_reminder_ids" in message
                    or not message["text"].startswith(f"@{recipient} T{check['id']} is due.\n")):
                raise ValueError("Timed to-do checks require a notice to their recipient")
        if "todo_answer_targets" in message:
            targets = message["todo_answer_targets"]
            if (message["kind"] != "msg" or message["sender"] != USER or "reply_to" not in message
                    or not isinstance(targets, list) or not targets):
                raise ValueError("To-do answer targets require a user reply")
            for target in targets:
                todo.validate_answer_target(target, message["id"])
        if "todo_answer" in message:
            cue = message["todo_answer"]
            if (message["kind"] != "msg" or message["sender"] != "system"
                    or not isinstance(cue, dict) or cue.keys() != {"id", "revision", "owner", "answer"}):
                raise ValueError("To-do answer cues require a system message")
            todo.positive_id(cue["answer"], "To-do answer ID")
            if cue["answer"] >= message["id"]:
                raise ValueError("The answer must precede its cue")
            todo.validate_answer_target({key: value for key, value in cue.items() if key != "answer"}, cue["answer"])
        if "todo_next" in message:
            if message["kind"] != "system" or message["sender"] != "system":
                raise ValueError("Next to-do changes require a system record")
            event = message["todo_next"]
            todo.validate_next(event)
            if event["todo_id"] is not None and event["todo_id"] >= message["id"]:
                raise ValueError("Next to-do must precede its selection record")
        if "task_helper" in message:
            event = message["task_helper"]
            if (message["kind"] != "system" or message["sender"] != "system"
                    or not isinstance(event, dict) or set(event) != {"task_id", "agent", "action", "todo_id"}
                    or event["agent"] not in AGENTS or event["action"] not in ("join", "leave")):
                raise ValueError("Invalid task helper record")
            if type(event["task_id"]) is not int or not 0 < event["task_id"] < message["id"]:
                raise ValueError("Helper task ID must precede its record")
            item_id = event["todo_id"]
            if item_id is not None and (event["action"] == "leave" or type(item_id) is not int
                                        or not 0 < item_id < message["id"]):
                raise ValueError("Invalid helper to-do ID")
        if "decision_prompt" in message:
            if message["kind"] != "msg" or message["sender"] not in AGENTS:
                raise ValueError("A decision question must be an agent message")
            decisions.validate_prompt(message["decision_prompt"])
        return message
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid log record at {location}: {error}") from error


def _file_tail(path, limit):
    chunks, newlines = [], 0
    with path.open("rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        while position and newlines <= limit:
            start = max(0, position - 8192)
            stream.seek(start)
            chunk = stream.read(position - start)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
            position = start
    # The final split element is either empty or an uncommitted suffix.
    lines = b"".join(reversed(chunks)).split(b"\n")[:-1][-limit:]
    rows = [_parse_record(line, f"{path.name}:tail") for line in lines]
    if any(a["id"] >= b["id"] for a, b in zip(rows, rows[1:])):
        raise ValueError(f"Invalid log record at {path.name}: tail ids must increase")
    return rows


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix=".agi-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _atomic_json(path, value):
    _atomic_write(path, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))


def _repair_tails():
    # A newline is the record's commit marker. Never alter committed records.
    for path in CHAT_DIR.glob("*.jsonl"):
        with path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if not size:
                continue
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) == b"\n":
                continue
            # Find the previous newline without loading the entire daily file.
            position = size
            boundary = 0
            while position:
                start = max(0, position - 8192)
                stream.seek(start)
                chunk = stream.read(position - start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    boundary = start + newline + 1
                    break
                position = start
            stream.seek(boundary)
            suffix = stream.read()
            recovery = DATA_DIR / "recovery"
            recovery.mkdir(exist_ok=True, mode=0o700)
            _sync_directory(DATA_DIR)
            name = recovery / f"{path.name}.{time.time_ns()}.partial"
            _atomic_write(name, suffix)
            stream.truncate(boundary)
            stream.flush()
            os.fsync(stream.fileno())


def _summary(messages):
    """Reuse derived state; fold only appended records when the prefix survives."""
    global _summary_cache
    with _cache_lock:
        if _summary_cache is not None and _summary_cache[0] is messages:
            return _summary_cache[1]
        previous = _summary_cache
        incremental = (previous is not None and len(messages) >= len(previous[0])
                       and all(a is b for a, b in zip(messages, previous[0])))
        if incremental:
            old = previous[1]
            result = dict(old, claims=dict(old["claims"]), tasks=dict(old["tasks"]), times=list(old["times"]),
                          rate_notices=list(old["rate_notices"]), reminders=dict(old["reminders"]),
                          reminded=set(old["reminded"]), todos=dict(old["todos"]), todo_checks=dict(old["todo_checks"]),
                          task_helpers=dict(old["task_helpers"]), task_actions=dict(old["task_actions"]),
                          next_todos=dict(old["next_todos"]), decisions=dict(old["decisions"]),
                          todo_answers=dict(old["todo_answers"]))
            tail = messages[len(previous[0]):]
        else:
            result = {"streak": 0, "noticed": False, "claims": {}, "tasks": {},
                      "times": [], "rate_notices": [], "reminders": {}, "reminded": set(), "todos": {},
                      "todo_checks": {}, "task_helpers": {}, "task_actions": {}, "next_todos": {}, "decisions": {},
                      "todo_answers": {}}
            tail = messages
        now = time.time()
        helpers = result["task_helpers"]
        actions = result["task_actions"]
        next_todos = result["next_todos"]
        items = result["todos"]
        todo_checks = result["todo_checks"]
        todo_answers = result["todo_answers"]
        for message in tail:
            sender, kind, text = message["sender"], message["kind"], message["text"]
            reminder = message.get("task_reminder")
            if reminder is not None:
                result["reminders"][reminder["id"]] = dict(reminder, event_id=message["id"],
                                                              event_ts=message["ts"])
            result["reminded"].update(message.get("task_reminder_ids", ()))
            if "todo_event" in message:
                prior_due = items.get(message["todo_event"]["id"], {}).get("due_at")
                item = todo.apply_event(items, message)
                todo_answers.pop(item["id"], None)
                if item["due_at"] is None or item["result"] is not None:
                    todo_checks.pop(item["id"], None)
                elif item["due_at"] != prior_due:
                    todo_checks[item["id"]] = item["revision"]
                for owner, choice in tuple(next_todos.items()):
                    if choice == item["id"] and (owner != item["owner"] or item["result"] is not None):
                        del next_todos[owner]
                if item["result"] is not None:
                    _finish_deferred_tasks(result, item, message)
            if "todo_check" in message:
                check = message["todo_check"]
                item_id = check["id"]
                schedule_revision = todo_checks.get(item_id)
                if schedule_revision is None or check != todo.check_receipt(items[item_id], schedule_revision):
                    raise ValueError(f"Invalid timed check for T{item_id}")
                del todo_checks[item_id]
            for target in message.get("todo_answer_targets", ()):
                item = items.get(target["id"])
                if (item is not None and item["revision"] == target["revision"] and item["result"] is None
                        and item["source_id"] == message["reply_to"] and item["next_actor"] == USER
                        and item["owner"] == target["owner"]):
                    todo_answers[item["id"]] = dict(target, answer=message["id"])
            if (cue := message.get("todo_answer")) is not None and todo_answers.get(cue["id"]) == cue:
                del todo_answers[cue["id"]]
            selection = message.get("todo_next")
            if selection is not None:
                if selection["todo_id"] is None:
                    next_todos.pop(selection["owner"], None)
                else:
                    next_todos[selection["owner"]] = selection["todo_id"]
            helper = message.get("task_helper")
            if helper is not None:
                role = (helper["task_id"], helper["agent"])
                if helper["action"] == "join":
                    helpers[role] = message
                else:
                    helpers.pop(role, None)
            if "decision_prompt" in message:
                result["decisions"][message["id"]] = dict(message["decision_prompt"], question_id=message["id"],
                                                          selections={}, review_answer_id=None)
            if kind == "msg" and sender == USER:
                result["streak"], result["noticed"] = 0, False
                question_id = message.get("reply_to") or _reply_id(text)
                question = result["decisions"].get(question_id)
                if question is not None:
                    body = _REPLY_PREFIX.sub("", text, count=1)
                    result["decisions"][question_id] = decisions.apply_reply(question, body, message["id"])
            elif kind == "msg" and sender in GUARDED_SENDERS:
                result["streak"] += 1
                result["times"].append(_timestamp(message, now))
            elif kind == "system" and sender == "system":
                if text.startswith(_GUARD_NOTICE):
                    result["noticed"] = True
                elif text.startswith(_RATE_NOTICE):
                    result["rate_notices"].append(_timestamp(message, 0))
            if (revoked := message.get("task_revocation")) is not None:
                key = f"task:{revoked['task_id']}"
                prior = result["tasks"].get(key, {})
                if (prior.get("id") != revoked["claim_id"]
                        or result["claims"].get(key) != revoked["owner"]):
                    raise ValueError(f"Task claim changed before release: {key}")
                del result["claims"][key]
                actions.pop(key, None)
                result["tasks"][key] = dict(message, kind="release", text=key)
            elif kind in ("claim", "release"):
                claims, tasks = result["claims"], result["tasks"]
                task = TASK_KEY.fullmatch(text) is not None
                if message.get("doing_update"):
                    if claims.get(text) == sender:
                        actions[text] = message
                    continue
                if kind == "claim":
                    if task and tasks.get(text, {}).get("done"):
                        continue
                    claims[text] = sender
                elif claims.get(text) == sender:
                    del claims[text]
                else:
                    continue
                if task:
                    tasks[text] = message
                    if "doing" in message:
                        actions[text] = message
                    else:
                        actions.pop(text, None)
                    task_id = int(text[5:])
                    _refresh_following_todos(result, task_id, message)
                    if message.get("done"):
                        for agent in AGENTS:
                            helpers.pop((task_id, agent), None)
                    elif kind == "claim":
                        helpers.pop((task_id, sender), None)
        result["times"].sort()
        result["rate_notices"].sort()
        _summary_cache = (messages, result)
        return result


def _refresh_following_todos(summary, task_id, event):
    if event["kind"] != "claim" and not event.get("done"):
        return
    items = summary["todos"]
    for number, item in items.items():
        if not item.get("follow_request") or item["source_id"] != task_id or item["result"] is not None:
            continue
        if event.get("done"):
            result = event.get("result") or f"Request #{task_id} completed"
            changed = dict(item, result=result, next_actor=None)
            changed.pop("wait_for", None)
            summary["todo_checks"].pop(number, None)
        elif item["owner"] != event["sender"]:
            recipient = event["sender"] if item["next_actor"] == item["owner"] else item["next_actor"]
            changed = dict(item, owner=event["sender"], next_actor=recipient)
        else:
            continue
        if summary["next_todos"].get(item["owner"]) == number:
            del summary["next_todos"][item["owner"]]
        summary["todo_answers"].pop(number, None)
        items[number] = dict(changed, revision=event["id"], updated_at=event["ts"])


def _finish_deferred_tasks(summary, item, completion):
    """Project completion from the same durable record that closes the last linked item."""
    for key, release in summary["tasks"].items():
        selected = release.get("deferred_todos", ())
        if (release.get("done") or item["id"] not in selected
                or any(summary["todos"][number]["result"] is None for number in selected)):
            continue
        event = dict(release, id=completion["id"], ts=completion["ts"], done=True, result=item["result"])
        summary["tasks"][key] = event
        _refresh_following_todos(summary, int(key[5:]), event)
        for agent in AGENTS:
            summary["task_helpers"].pop((int(key[5:]), agent), None)


def _timestamp(message, fallback):
    try:
        return datetime.fromisoformat(message["ts"]).timestamp()
    except (ValueError, OverflowError, OSError):
        return fallback


def _autonomy_expiry(now):
    try:
        value = json.loads((DATA_DIR / "autonomy.json").read_text())["expires_at"]
        if type(value) in (int, float) and math.isfinite(value) and now < value <= now + 86400:
            return value
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        pass
    return None


def _policy(mode="normal", *, start_after_id=0, **values):
    limit, window = {"normal": (5, 60), "wait": (GUARD_LIMIT, None),
                     "paused": (0, None), "custom": (None, None)}[mode]
    return dict(version=1, mode=mode, limit=limit, window_s=window, expires_at=None,
                start_after_id=start_after_id, **values)


def _validate_policy(value, *, nested=False):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value['version'] != 1:
        raise ValueError("Invalid policy version")
    mode = value.get("mode")
    if mode not in ("normal", "custom", "wait", "paused") or (nested and mode == "paused"):
        raise ValueError("Invalid policy mode")
    _integer(value.get("start_after_id"), "policy start id")
    _integer(value.get("limit"), "message limit")
    policy = dict(value)
    if mode == "custom":
        if value['limit'] < 1:
            raise ValueError("Message limit must be positive")
        window = value.get('window_s')
        if type(window) not in (int, float) or not 0 < window:
            raise ValueError("Invalid policy window_s")
        if window > 86400:
            raise ValueError("Policy window exceeds one day")
        expires = value['expires_at']
        if expires is not None:
            if type(expires) not in (int, float) or not 0 < expires:
                raise ValueError("Invalid policy expires_at")
            if expires > time.time() + 86400:
                raise ValueError('Policy expiry exceeds one day')
    else:
        default = _policy(mode)
        parameters = (value['limit'], value.get('window_s'), value.get('expires_at'))
        if mode == 'normal' and parameters in ((4, 60, None), (10, 60, None)):
            policy['limit'] = default['limit']  # Earlier built-in Normal rates.
        elif parameters != (default['limit'], default['window_s'], None):
            raise ValueError("Invalid fixed policy parameters")
    if mode == "paused":
        policy['resume'] = _validate_policy(value.get('resume'), nested=True)
    return policy


def _custom_expired(policy, now):
    return policy['mode'] == 'custom' and policy['expires_at'] is not None and policy['expires_at'] <= now


def _current_policy(now):
    try:
        value = _validate_policy(json.loads((DATA_DIR / "policy.json").read_text()))
    except FileNotFoundError:
        expires = _autonomy_expiry(now)
        return dict(_policy('custom'), limit=RATE_LIMIT, window_s=RATE_WINDOW, expires_at=expires) if expires else _policy()
    except (ValueError, KeyError, TypeError):
        return _policy('wait')  # damaged configuration must not silently resume quiet/auto work
    if _custom_expired(value, now):
        return _policy()
    return value


def _guard_state(messages, now=None):
    now = time.time() if now is None else now
    summary = _summary(messages)
    policy = _current_policy(now)
    if policy['start_after_id'] > (messages[-1]['id'] if messages else 0):
        policy = _policy('wait')
    mode, limit, window, expires = (policy[k] for k in ('mode', 'limit', 'window_s', 'expires_at'))
    result = dict(mode=mode, blocked=False, used=0, limit=limit, message_limit=limit,
                  window_seconds=window, expires_at=expires, retry_after=0,
                  agent_streak=summary['streak'])
    if mode == 'paused':
        noticed = False
        for message in reversed(messages):
            if message['id'] <= policy['start_after_id']:
                break
            if message['sender'] == 'system' and message['text'].startswith(_PAUSE_NOTICE):
                noticed = True
                break
        result.update(blocked=True, retry_after=None)
    elif mode == 'wait':
        used, noticed = 0, False
        for message in reversed(messages):
            if message['id'] <= policy['start_after_id'] or (message['kind'] == 'msg' and message['sender'] == USER):
                break
            if message['kind'] == 'msg' and message['sender'] in GUARDED_SENDERS:
                used += 1
            if message['sender'] == 'system' and message['text'].startswith(_GUARD_NOTICE):
                noticed = True
        result.update(used=used, blocked=used >= limit, retry_after=None if used >= limit else 0)
    else:
        times = summary['times']
        active = times[bisect_right(times, now-window):bisect_right(times, now)]
        blocked = len(active) >= limit
        delay = max(0, active[-limit] + window-now) if blocked else 0
        if expires is not None:
            delay = min(delay, expires-now)
        notices = summary['rate_notices']
        noticed = blocked and bool(notices) and notices[-1] >= active[-limit]
        result.update(used=len(active), blocked=blocked, retry_after=delay)
    return result, noticed


def _save_policy(policy, messages):
    _atomic_json(DATA_DIR / "policy.json", policy)
    description = f"Posting policy: {policy['mode']}"
    if policy['window_s'] is not None:
        description += f"; {policy['limit']} agent messages per {policy['window_s']:g} seconds"
    if policy['expires_at'] is not None:
        description += f"; until {datetime.fromtimestamp(policy['expires_at']).astimezone().isoformat()}"
    try:
        _write_record('system', description + '.', 'system', messages)
    except OSError:
        pass  # The policy itself is already durable; the notice is auxiliary.
    return _guard_state(_records())[0]


def set_policy(mode, *, limit=None, window_s=None, duration_s=None):
    """Set a posting policy. A custom budget lasts until changed unless a duration is supplied."""
    if not isinstance(mode, str):
        raise ValueError('Policy mode must be a string')
    if mode not in ('normal', 'custom', 'wait', 'paused'):
        raise ValueError("Policy mode must be normal, custom, wait or paused")
    if mode == 'custom':
        _integer(limit, 'message limit')
        if limit < 1:
            raise ValueError('Message limit must be positive')
        intervals = [('window', window_s)]
        if duration_s is not None:
            intervals.append(('duration', duration_s))
        for name, number in intervals:
            if type(number) not in (int, float) or not 0 < number <= 86400:
                raise ValueError(f'Policy {name} must be finite, positive and at most 86400 seconds')
    elif any(value is not None for value in (limit, window_s, duration_s)):
        raise ValueError('Only custom accepts custom rate/duration parameters')
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        now = time.time()
        current = _current_policy(now)
        if mode == 'paused' and current['mode'] == 'paused':
            return _guard_state(messages)[0]
        policy = _policy(mode, start_after_id=messages[-1]['id'] if messages else 0)
        if mode == 'custom':
            policy.update(limit=limit, window_s=window_s,
                          expires_at=now + duration_s if duration_s is not None else None)
        elif mode == 'paused':
            policy['resume'] = current
        return _save_policy(policy, messages)


def resume_policy():
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        now = time.time()
        current = _current_policy(now)
        if current['mode'] != 'paused':
            return _guard_state(messages)[0]
        policy = dict(current['resume'])
        if _custom_expired(policy, now):
            policy = _policy()
        policy['start_after_id'] = messages[-1]['id'] if messages else 0
        return _save_policy(policy, messages)


def _claims(messages):
    return dict(_summary(messages)["claims"])


def _main_root():
    """Recognize main-checkout absolute paths as well as this worktree's."""
    dotgit = CHECKOUT_ROOT / ".git"
    if dotgit.is_file():
        content = dotgit.read_text().strip()
        if content.startswith("gitdir: "):
            location = Path(content[8:])
            if not location.is_absolute():
                location = CHECKOUT_ROOT / location
            for parent in location.resolve().parents:
                if parent.name == ".git":
                    return parent.parent
    return CHECKOUT_ROOT


def _normalize_claim(text):
    path = Path(text).expanduser()
    roots = (CHECKOUT_ROOT.resolve(), _main_root().resolve())
    if not path.is_absolute():
        # Resolving catches both '..' escapes and symlinks pointing outside.
        resolved = (roots[0] / path).resolve()
        roots = roots[:1]
    else:
        resolved = path.resolve()
    for root in roots:
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            pass
    raise ValueError("Claim path must stay inside the project checkout")


def _overlaps(first, second):
    if first.startswith("task:") or second.startswith("task:"):
        return first == second
    a, b = Path(first).parts, Path(second).parts
    return a[:len(b)] == b or b[:len(a)] == a


def _write_record(sender, text, kind, messages, request_id=None, *, done=False, result=None, note=None,
                  task_reminder=None, task_reminder_ids=None, attachments=None, todo_event=None, task_helper=None,
                  todo_next=None, handoff_to=None, deferred_todos=None, doing=None, doing_update=False,
                  todo_wait=_UNCHANGED_WAIT, todo_ready=False, todo_check=None, decision_prompt=None,
                  check=None, skills=None, todo_answer=None, task_revocation=None):
    seq_path = DATA_DIR / "seq"
    seq = _integer(int(seq_path.read_text().strip()), "sequence") if seq_path.exists() else 0
    last = messages[-1]["id"] if messages else 0
    now = datetime.now().astimezone()
    message = {"id": max(seq, last) + 1, "ts": now.isoformat(),
               "sender": sender, "kind": kind, "text": text}
    if kind == "msg" and sender != "system":
        target = reply_target(message, lookup=lambda msg_id: _record_at(messages, msg_id))
        if target is not None:
            message["reply_to"] = target["id"]
            if sender == USER:
                summary = _summary(messages)
                matches = [dict(id=item["id"], revision=item["revision"], owner=item["owner"])
                           for item in summary["todos"].values()
                           if item["source_id"] == target["id"] and item["next_actor"] == USER
                           and item["result"] is None and item["owner"] in AGENTS
                           and _todo_can_notify(item, summary)]
                if matches:
                    message["todo_answer_targets"] = matches
    if request_id is not None:
        message["request_id"] = request_id
    if attachments:
        message["attachments"] = attachments
    if done:
        message["done"] = True
    if check is not None:
        message.update(result=result, check=check, skills=skills)
    if note is not None:
        message["note"] = note
    if doing is not None:
        message["doing"] = doing
    if doing_update:
        message["doing_update"] = True
    if handoff_to is not None:
        message["handoff_to"] = handoff_to
    if deferred_todos is not None:
        message["deferred_todos"] = list(deferred_todos)
    if task_reminder is not None:
        message["task_reminder"] = dict(task_reminder)
    if task_reminder_ids:
        message["task_reminder_ids"] = list(task_reminder_ids)
    if todo_event is not None:
        message["todo_event"] = todo_event
    if todo_wait is not _UNCHANGED_WAIT:
        message["todo_wait"] = todo_wait
    if todo_ready:
        message["todo_ready"] = True
    if todo_check is not None:
        message["todo_check"] = dict(todo_check)
    if todo_answer is not None:
        message["todo_answer"] = dict(todo_answer)
    if task_helper is not None:
        message["task_helper"] = task_helper
    if task_revocation is not None:
        message["task_revocation"] = task_revocation
    if todo_next is not None:
        message["todo_next"] = todo_next
    if decision_prompt is not None:
        message["decision_prompt"] = decision_prompt
    path = CHAT_DIR / (now.date().isoformat() + ".jsonl")
    data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise OSError("Short write to chat log")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory(CHAT_DIR)
    # The log is now durable. Auxiliary sequence metadata must not turn a
    # committed message into a false failure (and invite a duplicate retry).
    try:
        _atomic_write(seq_path, f"{message['id']}\n".encode())
    except OSError:
        pass  # The next writer also reads the committed log's highest id.
    return message


def _sync_committed_logs():
    # A complete line may be visible after an interrupted fsync. Before an
    # idempotent retry is confirmed, establish durability rather than guessing.
    for path in CHAT_DIR.glob("*.jsonl"):
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    _sync_directory(CHAT_DIR)


def find_request(request_id):
    _request_id(request_id)
    with _locked():
        message = next((m for m in _records() if m.get("request_id") == request_id), None)
        if message is not None:
            _sync_committed_logs()
            return _copy_message(message)
    return None


def _without_inline_code(line):
    # Match whole backtick runs using their lengths, not a backtracking regex.
    # An unmatched run stays prose; an equal closing run ends the code span.
    runs = list(re.finditer(r"`+", line))
    next_run, following = {}, {}
    for index in range(len(runs)-1, -1, -1):
        size = runs[index].end() - runs[index].start()
        if size in following:
            next_run[index] = following[size]
        following[size] = index
    result, cursor, index = [], 0, 0
    while index < len(runs):
        if index in next_run:
            end = next_run[index]
            result.append(line[cursor:runs[index].start()])
            result.append(" ")  # removing code must not manufacture a mention
            cursor, index = runs[end].end(), end+1
        else:
            index += 1
    result.append(line[cursor:])
    return ''.join(result)


def mentioned_agents(text):
    """Whole agent mentions in prose; quoted/code examples do not address an agent.

    This is deliberately a small routing rule, not a full Markdown renderer.
    Unclosed fenced blocks remain code; unmatched inline backticks remain prose.
    """
    result = mentioned(text, ('claude', 'codex', 'all'))
    if 'all' in result:
        result.remove('all')
        result.update(AGENTS)
    return result


def _prose_lines(text, *, keep_inline_code=False):
    """Yield lines outside quotes and code blocks."""
    fence = None
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
            continue
        if match:
            fence = (match[1][0], len(match[1]))
            continue
        if line.lstrip().startswith('>') or line.startswith(('    ', '\t')):
            continue
        yield line if keep_inline_code else _without_inline_code(line)


def mentioned(text, names):
    """Which of `names` are @-mentioned in prose, lowercased: the one rule for agents' routing and the user's
    highlight alike, so a mention in code, a quote or after a backslash never counts for either."""
    pattern = r"(?<![\w.\\])@(" + "|".join(re.escape(name) for name in names) + r")(?![\w-])"
    return {name.lower() for name in re.findall(pattern, '\n'.join(_prose_lines(text)), re.I)}


def _reply_id(text):
    reply = _REPLY_PREFIX.match(text)
    return int(reply[1]) if reply else None


def _record_at(records, msg_id):
    index = bisect_left(records, msg_id, key=lambda message: message["id"])
    if index < len(records) and records[index]["id"] == msg_id:
        return records[index]
    return None


def reply_target(message, *, lookup=None):
    """Resolve a direct reply to an earlier chat message using an optional local lookup."""
    if message["kind"] != "msg":
        return None
    target_id = message.get("reply_to") or _reply_id(message["text"])
    if not target_id or ("id" in message and target_id >= message["id"]):
        return None
    if lookup is None:
        rows = read_messages(target_id - 1, limit=1)
        target = rows[0] if rows else None
    else:
        target = lookup(target_id)
    if target is not None and target["id"] == target_id and target["kind"] == "msg":
        return target
    return None


def addressed_to(message, recipient, *, lookup=None):
    """Check a prose mention or a reply to a message by the recipient."""
    if (message["kind"] != "msg" and not message.get("todo_ready")) or message["sender"] == recipient:
        return False
    if "todo_check" in message:
        check = message["todo_check"]
        return check.get("recipient", check["owner"]) == recipient
    if "todo_answer" in message:
        return message["todo_answer"]["owner"] == recipient
    names = ("user", "you", "all") if recipient == USER else (recipient, "all")
    if mentioned(message["text"], names):
        return True
    target = reply_target(message, lookup=lookup)
    return target is not None and target["sender"] == recipient


def _task_doing(value):
    if (not isinstance(value, str) or not value.strip() or not value.strip().isprintable()
            or len(value.strip()) > MAX_TASK_DOING_CHARS):
        raise ValueError(f"Current action must contain 1–{MAX_TASK_DOING_CHARS} characters on one line")
    return value.strip()


def _task_hint(message, messages):
    target_id = message.get("reply_to")
    if target_id is None:
        return None
    summary = _summary(messages)
    key = f"task:{target_id}"
    claim = summary["tasks"].get(key)
    if claim is None or claim["kind"] != "claim" or claim["sender"] == message["sender"]:
        return None
    doing = summary["task_actions"].get(key, {}).get("doing")
    return {"id": target_id, "owner": claim["sender"], "doing": doing}


def _todo_hint(message):
    """Flag one possible future commitment for the posting agent to verify."""
    for line in _prose_lines(message["text"], keep_inline_code=True):
        prose = _QUOTED_PROSE.sub(" ", _without_inline_code(line))
        for sentence in _SENTENCE_GAP.split(prose):
            sentence = _PROMISE_PREFIX.sub("", sentence.strip()).strip("*_")
            if _PROMISE.match(sentence) and not _IMMEDIATE.search(sentence):
                return {"source_id": message.get("reply_to", message["id"]), "candidate": line.strip()}
    return None


def _with_receipt_hints(message, messages):
    if message["kind"] != "msg" or message["sender"] not in AGENTS:
        return message
    receipt = dict(message)
    if hint := _task_hint(message, messages):
        receipt["task_hint"] = hint
    if hint := _todo_hint(message):
        receipt["todo_hint"] = hint
    return receipt


def append_message(sender, text, kind="msg", *, request_id=None, attachments=None, doing=None, with_hints=False,
                   decision_prompt=None):
    if sender not in SENDERS:
        raise ValueError(f"Unknown sender: {sender}")
    if kind not in KINDS:
        raise ValueError(f"Unknown message kind: {kind}")
    images = _attachments(attachments)
    if not isinstance(text, str) or (not text.strip() and not images):
        raise ValueError("Message text must not be empty")
    if sender == ADMIN and (kind != "msg" or images):
        raise ValueError("Admin posts require a text-only message")
    if images and (kind != "msg" or sender == "system"):
        raise ValueError("Attachments require a participant message")
    if request_id is not None:
        _request_id(request_id)
        if kind != "msg" or sender == "system":
            raise ValueError("request_id is only valid for participant messages")
    if decision_prompt is not None:
        if kind != "msg" or sender not in AGENTS or not text.strip():
            raise ValueError("A decision question must be a text message from an agent")
        decisions.validate_prompt(decision_prompt)
        decision_prompt = dict(decision_prompt, choices=list(decision_prompt["choices"]))
    task_id = None
    if kind in ("claim", "release"):
        text = text.strip()
        if text.startswith("task:"):
            number = text[5:]
            if not number.isascii() or not number.isdigit() or int(number) < 1:
                raise ValueError("Task claim must use task:<positive user message id>")
            task_id = int(number)
            text = f"task:{task_id}"
            if sender == USER:
                raise ValueError("Task ownership is for agents; use a file path for a user claim")
            _agent(sender)
            if kind == "release":
                raise ValueError("Use done or release-task to record the task's result, check and skills")
        else:
            text = _normalize_claim(text)
            if kind == "claim" and text.startswith("task:"):
                # Older path-shaped claims can still be released by their owner.
                raise ValueError("File claims cannot use the reserved task: namespace")
    if doing is not None:
        if kind != "claim" or task_id is None:
            raise ValueError("Current action requires an agent task claim")
        doing = _task_doing(doing)
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        if request_id is not None:
            existing = next((m for m in messages if m.get("request_id") == request_id), None)
            if existing is not None:
                if ((existing["sender"], existing["kind"], existing["text"]) != (sender, kind, text)
                        or existing.get("attachments", []) != images
                        or existing.get("decision_prompt") != decision_prompt):
                    raise ValueError("request_id was already used for different message content")
                _sync_committed_logs()
                receipt = _copy_message(existing)
                return _with_receipt_hints(receipt, messages) if with_hints else receipt
        if task_id is not None and kind == "claim":
            source = _record_at(messages, task_id)
            if source is None or source["sender"] != USER or source["kind"] != "msg":
                raise ValueError(f"Task {task_id} must refer to a user message")
            summary = _summary(messages)
            prior = summary["tasks"].get(text, {})
            if prior.get("done"):
                raise ClaimConflict(f"Task {task_id} was completed by {prior['sender']}")
            if doing is None and prior.get("kind") == "claim" and prior.get("sender") == sender:
                doing = summary["task_actions"].get(text, {}).get("doing")
            if prior.get("handoff_to"):
                assigned = {prior["handoff_to"]}
            elif prior.get("task_revocation"):
                assigned = set()
            else:
                assigned = mentioned_agents(source["text"])
                target = reply_target(source, lookup=lambda number: _record_at(messages, number))
                if target is not None and target["sender"] in AGENTS:
                    assigned.add(target["sender"])
            if len(assigned) == 1 and sender not in assigned:
                raise ClaimConflict(f"Task {task_id} is addressed to {next(iter(assigned))}. "
                                    "Join as a helper once it is claimed, or ask for a handoff.")
        status, noticed = _guard_state(messages)
        if kind == "msg" and sender in GUARDED_SENDERS and status["blocked"]:
            if status['mode'] in ('normal', 'custom'):
                prefix = _RATE_NOTICE
                reason = f"retry after {math.ceil(status['retry_after'])} seconds"
            elif status['mode'] == 'paused':
                prefix, reason = _PAUSE_NOTICE, 'posting is paused; use /resume or change policy'
            else:
                prefix, reason = _GUARD_NOTICE, 'wait for a user message'
            if not noticed:
                _write_record('system', f'{prefix}{sender} was refused; {reason}.', 'system', messages)
            raise GuardBlocked(f'Agent message limit reached; {reason}')
        if kind in ("claim", "release"):
            claims = _claims(messages)
            if kind == "claim":
                for held, holder in claims.items():
                    if holder != sender and _overlaps(text, held):
                        raise ClaimConflict(f"{text} overlaps {held}, held by {holder}")
            else:
                _require_claim(claims, text, sender)
        for ref in images:
            _check_attachment_file(ref)
        record = _write_record(sender, text, kind, messages, request_id, attachments=images, doing=doing,
                               decision_prompt=decision_prompt)
        return _with_receipt_hints(record, messages) if with_hints else record


def take_task(agent, msg_id, *, doing=None):
    """Atomically claim a user request before announcing or starting work."""
    _agent(agent)
    _integer(msg_id, "task id")
    if msg_id == 0:
        raise ValueError("Task id must be positive")
    return append_message(agent, f"task:{msg_id}", kind="claim", doing=doing)


def set_task_doing(agent, msg_id, doing):
    """Update the current action without taking ownership from another agent."""
    _agent(agent)
    todo.positive_id(msg_id, "Task ID")
    doing = _task_doing(doing)
    key = f"task:{msg_id}"
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        _require_claim(summary["claims"], key, agent)
        prior = summary["task_actions"].get(key, {})
        if prior.get("doing") == doing:
            _sync_committed_logs()
            return _copy_message(prior)
        return _write_record(agent, key, "claim", messages, doing=doing, doing_update=True)


def _require_claim(claims, text, sender):
    holder = claims.get(text)
    if holder != sender:
        raise ClaimConflict(f"{text} is held by {holder}" if holder else f"No active claim for {text}")


def _task_evidence(result, check, skills):
    values = {"result": result, "check": check, "skills": skills}
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Task release requires {name}")
    values = {name: value.strip() for name, value in values.items()}
    if re.match(r"none\b", values["skills"], re.IGNORECASE) and not re.fullmatch(
            r"none,\s+because\s+\S.*", values["skills"], re.IGNORECASE | re.DOTALL):
        raise ValueError("Give the reason as 'none, because …' when no skills apply")
    return values


def release_task(agent, msg_id, *, done=False, result=None, check=None, skills=None, note=None, handoff_to=None):
    """Release a request for handoff or explicitly complete it as its current owner."""
    evidence = _task_evidence(result, check, skills)
    return _release_task(agent, msg_id, done=done, evidence=evidence, note=note, handoff_to=handoff_to)


def task_claim(task_id):
    """Return the exact active claim a person can show and confirm."""
    todo.positive_id(task_id, "Task ID")
    key = f"task:{task_id}"
    with _locked():
        summary = _summary(_records())
        owner = summary["claims"].get(key)
        if owner is None:
            return None
        event = summary["tasks"][key]
        return dict(claim_id=event["id"], owner=owner, since=event["ts"],
                    doing=summary["task_actions"].get(key, {}).get("doing"))


def release_task_claim(task_id, claim_id):
    """Release the claim shown to the user, and preserve its history."""
    todo.positive_id(task_id, "Task ID")
    todo.positive_id(claim_id, "Claim ID")
    key = f"task:{task_id}"
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        owner = summary["claims"].get(key)
        event = summary["tasks"].get(key, {})
        previous = event.get("task_revocation")
        if previous is not None and previous["claim_id"] == claim_id:
            _sync_committed_logs()
            return _copy_message(_record_at(messages, event["id"]))
        if owner is None or event["id"] != claim_id:
            raise ClaimConflict(f"The active claim for task #{task_id} changed.")
        return _write_record("system", f"You released {owner}'s claim on task #{task_id}.", "system", messages,
                             task_revocation=dict(task_id=task_id, claim_id=claim_id, owner=owner))


def defer_task(agent, msg_id, todo_ids, *, result=None, check=None, skills=None, note=None):
    """Release the driver's claim into the explicitly selected open to-dos."""
    if not isinstance(todo_ids, (list, tuple)) or not todo_ids:
        raise ValueError("Choose at least one open to-do for the remaining task work")
    for todo_id in todo_ids:
        todo.positive_id(todo_id, "To-do ID")
    if len(set(todo_ids)) != len(todo_ids):
        raise ValueError("Each deferred to-do may be selected once")
    evidence = _task_evidence(result, check, skills)
    return _release_task(agent, msg_id, evidence=evidence, note=note, deferred_todos=sorted(todo_ids))


def _release_task(agent, msg_id, *, evidence, done=False, note=None, handoff_to=None, deferred_todos=None):
    _agent(agent)
    _integer(msg_id, "task id")
    if msg_id == 0:
        raise ValueError("Task id must be positive")
    if type(done) is not bool:
        raise ValueError("Task completion must be a boolean")
    if note is not None and (done or not isinstance(note, str) or not note.strip()):
        raise ValueError("A handoff note must contain text and cannot accompany completion")
    if note is not None:
        note = note.strip()
        if len(note) > MAX_HANDOFF_NOTE_CHARS:
            raise ValueError(f"Handoff note can contain at most {MAX_HANDOFF_NOTE_CHARS} characters")
    if handoff_to is not None and (handoff_to not in AGENTS or handoff_to == agent or done):
        raise ValueError("A named handoff must release the request to the other agent")
    key = f"task:{msg_id}"
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        prior = summary["tasks"].get(key, {})
        if (prior.get("kind") == "release" and prior["sender"] == agent and prior.get("done", False) == done
                and prior.get("note") == note and prior.get("handoff_to") == handoff_to
                and prior.get("deferred_todos") == deferred_todos
                and all(prior.get(name) == text for name, text in evidence.items())):
            _sync_committed_logs()
            return _copy_message(prior)
        if prior.get("done"):
            raise ClaimConflict(f"Task {msg_id} was completed by {prior['sender']}")
        _require_claim(summary["claims"], key, agent)
        for todo_id in deferred_todos or ():
            item = summary["todos"].get(todo_id)
            if item is None or item["result"] is not None:
                raise ValueError(f"No open to-do T{todo_id}")
            if item.get("follow_request"):
                raise ValueError(f"T{todo_id} follows a request. Use an independent to-do to defer a task.")
        return _write_record(agent, key, "release", messages, done=done, note=note,
                             handoff_to=handoff_to, deferred_todos=deferred_todos, **evidence)


def _set_task_helper(agent, msg_id, action, todo_id=None):
    _agent(agent)
    todo.positive_id(msg_id, "Task ID")
    if todo_id is not None:
        todo.positive_id(todo_id, "To-do ID")
    key = f"task:{msg_id}"
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        prior = summary["task_helpers"].get((msg_id, agent))
        if action == "leave":
            if prior is None:
                _sync_committed_logs()
                return None
            text = f"{agent} left task #{msg_id}"
        else:
            if prior is not None and (todo_id is None or prior["task_helper"]["todo_id"] == todo_id):
                _sync_committed_logs()
                return _copy_message(prior)
            driver = summary["claims"].get(key)
            if driver is None:
                raise ClaimConflict(f"Task #{msg_id} has no current driver")
            if driver == agent:
                raise ClaimConflict(f"{agent} already drives task #{msg_id}")
            if todo_id is not None:
                item = summary["todos"].get(todo_id)
                if item is None or item["result"] is not None or item["owner"] != agent or item["source_id"] != msg_id:
                    raise ValueError("Link an open to-do you own that points to this task")
            text = f"{agent} joined task #{msg_id}"
        event = dict(task_id=msg_id, agent=agent, action=action, todo_id=todo_id)
        return _write_record("system", text, "system", messages, task_helper=event)


def join_task(agent, msg_id, *, todo_id=None):
    """Join another driver's task as a helper, optionally linking a to-do."""
    return _set_task_helper(agent, msg_id, "join", todo_id)


def leave_task(agent, msg_id):
    """Leave the caller's helper role without releasing the driver's claim."""
    return _set_task_helper(agent, msg_id, "leave")


def list_tasks():
    """Return tracked user requests and their most recent task outcomes."""
    with _locked():
        messages = _records()
        summary = _summary(messages)
        reminders = {event["task_id"]: event["due_at"]
                     for reminder_id, event in summary["reminders"].items()
                     if event.get("trigger", "time") == "time" and event["state"] == "scheduled"
                     and reminder_id not in summary["reminded"]}
        gate_waits = {event["task_id"]: {"project": gate_reminders.display_name(event["project"]),
                                          "commit": event["commit"]}
                      for reminder_id, event in summary["reminders"].items()
                      if event.get("trigger") == "gate" and event["state"] == "scheduled"
                      and reminder_id not in summary["reminded"]}
        rows = []
        for key, event in summary["tasks"].items():
            msg_id = int(key[5:])
            end = bisect_right(messages, msg_id, key=lambda message: message["id"])
            source = messages[end-1] if end else None
            if source is None or source["id"] != msg_id or source["sender"] != USER or source["kind"] != "msg":
                continue
            state = "done" if event.get("done") else {"claim": "claimed", "release": "released"}[event["kind"]]
            row = dict(id=msg_id, text=source["text"], ts=source["ts"], state=state,
                       owner=event["sender"] if state != "released" else None,
                       result=event.get("result"), event_id=event["id"],
                       event_ts=event["ts"])
            if "check" in event and (state != "done" or not event.get("deferred_todos")):
                row.update(check=event["check"], skills=event["skills"])
            if key in summary["task_actions"]:
                row["doing"] = summary["task_actions"][key]["doing"]
            roles = {agent: summary["task_helpers"][(msg_id, agent)]["task_helper"]["todo_id"]
                     for agent in AGENTS if (msg_id, agent) in summary["task_helpers"]}
            row["helpers"] = sorted(roles)
            row["helper_todos"] = {agent: item_id for agent, item_id in roles.items() if item_id is not None}
            if state == "released" and event.get("note"):
                row["note"] = event["note"]
            if state == "released" and event.get("handoff_to"):
                row["handoff_to"] = event["handoff_to"]
            if event.get("deferred_todos"):
                row["deferred_todos"] = list(event["deferred_todos"])
                if state == "released" and "note" not in row:
                    row["note"] = "Waiting for " + ", ".join(f"T{number}" for number in row["deferred_todos"])
            if state != "done" and msg_id in reminders:
                row["reminder_due"] = reminders[msg_id]
            if state != "done" and msg_id in gate_waits:
                row["reminder_gate"] = dict(gate_waits[msg_id])
            rows.append(row)
    return sorted(rows, key=lambda row: row["event_id"], reverse=True)


def _validate_todo_dependency(items, item_id, dependency):
    kind, _, target = todo.wait_target(dependency)
    visited = {item_id}
    while kind == "todo":
        if target in visited:
            raise ValueError("A to-do cannot wait for itself, even through other to-dos")
        visited.add(target)
        item = items.get(target)
        if item is None:
            raise ValueError(f"No prerequisite to-do #{target}")
        if not item.get("wait_for"):
            return
        kind, _, target = todo.wait_target(item["wait_for"])


def _write_todo(actor, item_id, revision, changes, request_id):
    changes = dict(changes)
    wait = changes.pop("wait_for", _UNCHANGED_WAIT)
    if isinstance(wait, str) or (wait is None and not changes):
        changes["next_actor"] = None
    event = dict(actor=actor, id=item_id, revision=revision, changes=changes, request_id=request_id)
    todo.validate_event(event)
    if wait is not _UNCHANGED_WAIT and wait is not None:
        kind, project, target = todo.wait_target(wait)
        if kind == "gate":
            gate_reminders.validate_commit(project, target)
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        if request_id is not None:
            for index, record in enumerate(messages):
                prior = record.get("todo_event")
                if prior is None or prior["request_id"] != request_id:
                    continue
                if prior != event or record.get("todo_wait", _UNCHANGED_WAIT) != wait:
                    raise ValueError("To-do request ID was already used for a different change")
                _sync_committed_logs()
                at_write = _summary(messages[: index + 1])
                return todo.public_item(at_write["todos"][item_id or record["id"]], at_write["next_todos"])
        summary = _summary(messages)
        items = summary["todos"]
        if item_id is None:
            source_id = changes["source_id"]
            if source_id is not None:
                source = _record_at(messages, source_id)
                if source is None or source["kind"] != "msg" or source["sender"] == "system":
                    raise ValueError("To-do source must be a participant's chat message")
            if changes.get("follow_request"):
                if summary["claims"].get(f"task:{source_id}") != changes["owner"]:
                    raise ValueError("A following to-do needs a source request claimed by its owner")
                if changes.get("remind_next_actor"):
                    raise ValueError("A following to-do sends its check to the current claimant")
            text = f"To-do added · {changes['title']}"
        else:
            item = items.get(item_id)
            if item is None:
                raise ValueError(f"No to-do #{item_id}")
            if actor not in (item["owner"], USER):
                raise ValueError(f"To-do #{item_id} is owned by {item['owner']}")
            if item["revision"] != revision or item["result"] is not None:
                raise ValueError(f"To-do #{item_id} changed. Reload the list before editing.")
            if item.get("follow_request") and (changes.get("remind_next_actor")
                                               or changes.get("owner", item["owner"]) != item["owner"]):
                raise ValueError("The linked request determines this to-do's owner and check recipient")
            text = f"To-do completed · {item['title']}" if "result" in changes else f"To-do updated · {item['title']}"
            if wait is None and not item.get("wait_for"):
                raise ValueError(f"To-do #{item_id} has no prerequisite to clear")
            pending = item.get("wait_for") if wait is _UNCHANGED_WAIT else wait
            if changes.get("next_actor") is not None and pending is not None:
                raise ValueError("Clear the prerequisite before choosing a next actor")
        if wait is not _UNCHANGED_WAIT and wait is not None:
            _validate_todo_dependency(items, item_id, wait)
        record = _write_record("system", text, "system", messages, todo_event=event, todo_wait=wait)
        item = todo.apply_event(dict(items), record)
        return todo.public_item(item, summary["next_todos"])


def add_todo(actor, source_id, project, title, owner, next_action, *,
             next_actor=None, due_at=None, reference=None, wait_for=None, request_id=None, remind_next_actor=False,
             follow_request=False):
    """Record a commitment linked to a participant's chat message."""
    todo.positive_id(source_id, "Source ID")
    changes = dict(source_id=source_id, project=project, title=title, owner=owner,
                   next_action=next_action, next_actor=next_actor, due_at=due_at, reference=reference)
    if wait_for is not None:
        changes["wait_for"] = wait_for
    if remind_next_actor is not False:
        changes["remind_next_actor"] = remind_next_actor
    if follow_request is not False:
        changes["follow_request"] = follow_request
    return _write_todo(actor, None, None, changes, request_id)


def add_personal_todo(title, *, due_at=None, request_id=None):
    """Journal a to-do for the user with no source message."""
    if isinstance(title, str):
        title = title.strip()
    changes = dict(source_id=None, project="AGI", title=title, owner=USER,
                   next_action=title, next_actor=USER, due_at=due_at, reference=None)
    return _write_todo(USER, None, None, changes, request_id)


def update_todo(actor, todo_id, revision, *, request_id=None, **changes):
    """Revise an open commitment from a known revision."""
    if not changes.keys() <= todo.FIELDS:
        raise ValueError("Unknown or uneditable to-do field")
    return _write_todo(actor, todo_id, revision, changes, request_id)


def complete_todo(actor, todo_id, revision, result, *, request_id=None):
    """Close a commitment while retaining its source and outcome."""
    return _write_todo(actor, todo_id, revision, {"result": result}, request_id)


def set_next_todo(actor, todo_id=None, *, owner=None, request_id=None):
    """Select an open to-do for its owner, or clear the owner's selection."""
    if actor not in todo.PARTICIPANTS:
        raise ValueError("To-do actor must be user, claude or codex")
    todo.validate_request_id(request_id)
    if todo_id is None:
        owner = actor if owner is None else owner
    else:
        todo.positive_id(todo_id, "To-do ID")
        if owner is not None:
            raise ValueError("Specify an owner only when clearing the next to-do")
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        if request_id is not None:
            for index, record in enumerate(messages):
                prior = record.get("todo_next")
                if prior is None or prior["request_id"] != request_id:
                    continue
                if (prior["actor"] != actor or prior["todo_id"] != todo_id
                        or (todo_id is None and prior["owner"] != owner)):
                    raise ValueError("To-do request ID was already used for a different change")
                _sync_committed_logs()
                if todo_id is None:
                    return None
                at_write = _summary(messages[: index + 1])
                return todo.public_item(at_write["todos"][todo_id], at_write["next_todos"])
        summary = _summary(messages)
        item = None
        if todo_id is not None:
            item = summary["todos"].get(todo_id)
            if item is None or item["result"] is not None:
                raise ValueError(f"No open to-do T{todo_id}")
            owner = item["owner"]
        event = dict(actor=actor, owner=owner, todo_id=todo_id, request_id=request_id)
        todo.validate_next(event)
        if request_id is not None or summary["next_todos"].get(owner) != todo_id:
            text = f"To-do marked next · T{todo_id} {item['title']}" if item else f"Next to-do cleared · {owner}"
            _write_record("system", text, "system", messages, todo_next=event)
        else:
            _sync_committed_logs()
        return todo.public_item(item, {owner: todo_id}) if item else None


def list_todos(*, include_done=False):
    """Return open commitments in insertion order, optionally including resolved ones."""
    with _locked():
        summary = _summary(_records())
        return [todo.public_item(item, summary["next_todos"]) for item in summary["todos"].values()
                if include_done or item["result"] is None]


def list_decisions():
    """Return marked questions with the latest explicit answer to each choice."""
    with _locked():
        rows = _summary(_records())["decisions"].values()
        return [dict(row, choices=list(row["choices"]),
                     selections={number: dict(pick) for number, pick in row["selections"].items()}) for row in rows]


def _reminder_time(value):
    if not _finite_nonnegative_number(value):
        raise ValueError("Reminder time must be a finite non-negative Unix timestamp")
    return float(value)


def _reminder_actor(actor):
    if actor not in (USER,) + AGENTS:
        raise ValueError(f"Unknown reminder actor: {actor}")


def _reminder_owner(summary, task_id):
    key = f"task:{task_id}"
    task = summary["tasks"].get(key)
    owner = summary["claims"].get(key)
    if task is None:
        raise ValueError(f"Task {task_id} has no claim history")
    if task.get("done"):
        raise ClaimConflict(f"Task {task_id} is already complete")
    if owner is None:
        raise ClaimConflict(f"Task {task_id} has no current owner")
    return owner


def _authorize_reminder_actor(actor, owner):
    if actor != USER and actor != owner:
        raise ClaimConflict(f"Only the task owner or user can change its reminder; current owner is {owner}")


def _active_task_reminder(summary, task_id):
    matches = [(reminder_id, event) for reminder_id, event in summary["reminders"].items()
               if event["task_id"] == task_id and event["state"] == "scheduled"
               and reminder_id not in summary["reminded"]]
    return matches[-1] if matches else (None, None)


def _latest_task_reminder(summary, task_id):
    matches = [(reminder_id, event) for reminder_id, event in summary["reminders"].items()
               if event["task_id"] == task_id]
    return max(matches, key=lambda pair: pair[1]["event_id"]) if matches else (None, None)


def _reminder_result(reminder_id, event):
    result = dict(id=reminder_id, task_id=event["task_id"], state=event["state"],
                  due_at=event.get("due_at"), note=event.get("note"),
                  event_id=event.get("event_id"), event_ts=event.get("event_ts"))
    if event.get("trigger") == "gate":
        result.update(trigger="gate", project=gate_reminders.display_name(event["project"]), commit=event["commit"])
    return result


def schedule_reminder(actor, task_id, due_at, note=None, *, reminder_id=None, preserve_deadline=False):
    """Create or reschedule one active reminder for an owned user task.

    User UI code may pass USER; an agent may only change reminders for a task it
    currently owns. The schedule event is durable in the append-only chat log.
    """
    _reminder_actor(actor)
    _integer(task_id, "task id")
    if task_id == 0:
        raise ValueError("Task id must be positive")
    due_at = _reminder_time(due_at)
    if reminder_id is not None and (not isinstance(reminder_id, str)
                                    or REMINDER_ID.fullmatch(reminder_id) is None):
        raise ValueError("Invalid reminder id")
    if note is not None:
        if not isinstance(note, str) or not note.strip() or len(note.strip()) > MAX_REMINDER_NOTE:
            raise ValueError(f"Reminder note must contain at most {MAX_REMINDER_NOTE} characters")
        note = note.strip()
    key = f"task:{task_id}"
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        owner = _reminder_owner(summary, task_id)
        _authorize_reminder_actor(actor, owner)
        active_id, active = _active_task_reminder(summary, task_id)
        if active is not None and active.get("trigger", "time") != "time":
            raise ClaimConflict(f"Task {task_id} already has a gate reminder {active_id}; cancel it first")
        if reminder_id is not None:
            prior = summary["reminders"].get(reminder_id)
            if prior is not None and prior["task_id"] != task_id:
                raise ClaimConflict(f"Reminder {reminder_id} belongs to a different task")
            if prior is not None and prior.get("trigger", "time") != "time":
                raise ClaimConflict(f"Reminder {reminder_id} is a gate reminder")
            active_id, _ = _active_task_reminder(summary, task_id)
            if prior is None and active_id is not None:
                raise ClaimConflict(f"Task {task_id} already has reminder {active_id}; reschedule that id")
            if prior is not None and preserve_deadline:
                if note != prior.get("note"):
                    raise ClaimConflict(f"Reminder {reminder_id} was already used with a different note")
                due_at = prior["due_at"]
            if prior is not None and (prior.get("due_at"), prior.get("note")) == (due_at, note):
                if reminder_id in summary["reminded"]:
                    prior = dict(prior, state="delivered")
                _sync_committed_logs()
                return _reminder_result(reminder_id, prior)
            if prior is not None and (prior["state"] == "cancelled" or reminder_id in summary["reminded"]):
                raise ClaimConflict(f"Reminder {reminder_id} has already ended; schedule a new reminder")
        else:
            reminder_id, prior = _active_task_reminder(summary, task_id)
            if reminder_id is None:
                delivered = [(candidate_id, event) for candidate_id, event in summary["reminders"].items()
                             if event["task_id"] == task_id and event["state"] == "scheduled"
                             and candidate_id in summary["reminded"]]
                if delivered:
                    candidate_id, event = max(delivered, key=lambda pair: pair[1]["event_id"])
                    if (event.get("due_at"), event.get("note")) == (due_at, note):
                        _sync_committed_logs()
                        return _reminder_result(candidate_id, dict(event, state="delivered"))
                reminder_id = uuid.uuid4().hex
        if prior is not None and (prior["due_at"] == due_at and prior.get("note") == note):
            _sync_committed_logs()
            return _reminder_result(reminder_id, prior)
        event = {"id": reminder_id, "task_id": task_id, "state": "scheduled", "due_at": due_at}
        if note is not None:
            event["note"] = note
        record = _write_record("system", f"Task reminder {reminder_id} scheduled.", "system", messages,
                               task_reminder=event)
        event.update(event_id=record["id"], event_ts=record["ts"])
        messages.append(record)
        summary["reminders"][reminder_id] = event
        return _reminder_result(reminder_id, event)


def schedule_gate_reminder(actor, task_id, project, commit, note=None, *, reminder_id=None):
    """Wait for an exact commit to become reachable from a project's local main ref.

    A gate reminder shares the task's single active reminder slot and cancellation
    path with timed reminders. Rebase changes a commit SHA; callers must schedule
    the final post-rebase candidate if that is the revision they intend to track.
    Git object validation runs before the global log lock is acquired.
    """
    _reminder_actor(actor)
    _integer(task_id, "task id")
    if task_id == 0:
        raise ValueError("Task id must be positive")
    if reminder_id is not None and (not isinstance(reminder_id, str)
                                    or REMINDER_ID.fullmatch(reminder_id) is None):
        raise ValueError("Invalid reminder id")
    if note is not None:
        if not isinstance(note, str) or not note.strip() or len(note.strip()) > MAX_REMINDER_NOTE:
            raise ValueError(f"Reminder note must contain at most {MAX_REMINDER_NOTE} characters")
        note = note.strip()
    project = gate_reminders.project_id(project)
    gate_reminders.validate_commit(project, commit)
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        owner = _reminder_owner(summary, task_id)
        _authorize_reminder_actor(actor, owner)
        active_id, prior = _active_task_reminder(summary, task_id)
        if prior is not None and prior.get("trigger", "time") != "gate":
            raise ClaimConflict(f"Task {task_id} already has timed reminder {active_id}; cancel it first")
        if reminder_id is not None:
            existing = summary["reminders"].get(reminder_id)
            if existing is not None and existing["task_id"] != task_id:
                raise ClaimConflict(f"Reminder {reminder_id} belongs to a different task")
            if existing is not None and existing.get("trigger", "time") != "gate":
                raise ClaimConflict(f"Reminder {reminder_id} is not a gate reminder")
            if existing is not None and (existing.get("project"), existing.get("commit"), existing.get("note")) == (project, commit, note):
                if reminder_id in summary["reminded"]:
                    existing = dict(existing, state="delivered")
                _sync_committed_logs()
                return _reminder_result(reminder_id, existing)
            if existing is not None and (existing["state"] == "cancelled" or reminder_id in summary["reminded"]):
                raise ClaimConflict(f"Reminder {reminder_id} has already ended; schedule a new reminder")
            if active_id is not None and active_id != reminder_id:
                raise ClaimConflict(f"Task {task_id} already has reminder {active_id}; reschedule that id")
            prior = existing
        elif prior is not None:
            reminder_id = active_id
            if (prior.get("project"), prior.get("commit"), prior.get("note")) == (project, commit, note):
                _sync_committed_logs()
                return _reminder_result(reminder_id, prior)
        else:
            delivered = [(candidate_id, event) for candidate_id, event in summary["reminders"].items()
                         if event["task_id"] == task_id and event.get("trigger") == "gate"
                         and event["state"] == "scheduled" and candidate_id in summary["reminded"]
                         and (event.get("project"), event.get("commit"), event.get("note")) == (project, commit, note)]
            if delivered:
                candidate_id, event = max(delivered, key=lambda pair: pair[1]["event_id"])
                _sync_committed_logs()
                return _reminder_result(candidate_id, dict(event, state="delivered"))
            reminder_id = uuid.uuid4().hex
        if prior is not None and (prior.get("project"), prior.get("commit"), prior.get("note")) == (project, commit, note):
            return _reminder_result(reminder_id, prior)
        event = {"id": reminder_id, "task_id": task_id, "state": "scheduled", "trigger": "gate",
                 "project": project, "commit": commit}
        if note is not None:
            event["note"] = note
        record = _write_record("system", f"Task {task_id} gate reminder {reminder_id} scheduled.", "system", messages,
                               task_reminder=event)
        event.update(event_id=record["id"], event_ts=record["ts"])
        messages.append(record)
        summary["reminders"][reminder_id] = event
        return _reminder_result(reminder_id, event)


def cancel_reminder(actor, task_id, reminder_id=None):
    """Cancel a scheduled task reminder; cancel and fire serialize on the log lock."""
    _reminder_actor(actor)
    _integer(task_id, "task id")
    if task_id == 0:
        raise ValueError("Task id must be positive")
    if reminder_id is not None and (not isinstance(reminder_id, str)
                                    or REMINDER_ID.fullmatch(reminder_id) is None):
        raise ValueError("Invalid reminder id")
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        if reminder_id is None:
            reminder_id, current = _active_task_reminder(summary, task_id)
            if current is None:
                reminder_id, current = _latest_task_reminder(summary, task_id)
        else:
            current = summary["reminders"].get(reminder_id)
        if current is None:
            raise ValueError(f"Task {task_id} has no reminder")
        if current["task_id"] != task_id:
            raise ClaimConflict(f"Reminder {reminder_id} belongs to a different task")
        task_key = f"task:{current['task_id']}"
        task = summary["tasks"].get(task_key)
        if task is None:
            raise ValueError(f"Task {current['task_id']} has no claim history")
        owner = summary["claims"].get(task_key)
        if actor != USER and actor != owner:
            raise ClaimConflict("Only the task owner or user can change its reminder")
        if reminder_id in summary["reminded"]:
            _sync_committed_logs()
            return _reminder_result(reminder_id, dict(current, state="delivered"))
        if current["state"] == "cancelled":
            _sync_committed_logs()
            return _reminder_result(reminder_id, current)
        if task.get("done"):
            return _reminder_result(reminder_id, dict(current, state="suppressed"))
        event = {"id": reminder_id, "task_id": current["task_id"], "state": "cancelled"}
        if current.get("trigger") == "gate":
            event.update(trigger="gate", project=current["project"], commit=current["commit"])
        elif current.get("due_at") is not None:
            event["due_at"] = current["due_at"]
        if current.get("note") is not None:
            event["note"] = current["note"]
        record = _write_record("system", f"Task reminder {reminder_id} cancelled.", "system", messages,
                               task_reminder=event)
        event.update(event_id=record["id"], event_ts=record["ts"])
        messages.append(record)
        summary["reminders"][reminder_id] = event
        return _reminder_result(reminder_id, event)


def _todo_can_notify(item, summary):
    return (not item.get("follow_request")
            or summary["claims"].get(f"task:{item['source_id']}") == item["owner"])


def next_reminder_deadline(now=None):
    """Return the next wall-clock dispatch deadline, or None if nothing can fire."""
    now = time.time() if now is None else _reminder_time(now)
    with _locked():
        messages = _records()
        summary = _summary(messages)
        items = summary["todos"]
        due = [items[item_id]["due_at"] for item_id in summary["todo_checks"]
               if todo.check_recipient(items[item_id]) and _todo_can_notify(items[item_id], summary)]
        for reminder_id, event in summary["reminders"].items():
            task = summary["tasks"].get(f"task:{event['task_id']}")
            if (event.get("trigger", "time") != "time" or event["state"] != "scheduled"
                    or reminder_id in summary["reminded"]
                    or task is None or task.get("done")):
                continue
            if summary["claims"].get(f"task:{event['task_id']}") is not None:
                due.append(event["due_at"])
        if not due:
            return None
        deadline = min(due)
        state, _ = _guard_state(messages, now)
        if state["blocked"]:
            if state["retry_after"] is None:
                return None
            deadline = max(deadline, now + state["retry_after"])
        return deadline


def _deliver_reminder_batch(messages, summary, due_by_owner):
    delivered = []
    for owner in AGENTS:
        reminders = due_by_owner[owner]
        if not reminders:
            continue
        reminders.sort(key=lambda pair: (pair[1].get("due_at", 0), pair[1]["task_id"], pair[0]))
        lines = [f"@{owner} Reminder: {len(reminders)} task reminder(s) are due."]
        for reminder_id, event in reminders:
            if event.get("trigger") == "gate":
                project = gate_reminders.display_name(event["project"])
                lines.append(f"Task #{event['task_id']} is waiting for {project} main to include commit {event['commit']}.")
            else:
                lines.append(f"Task #{event['task_id']} is due.")
            if event.get("note"):
                lines.extend("> " + line for line in event["note"].splitlines())
        record = _write_record("system", "\n".join(lines), "msg", messages,
                               task_reminder_ids=[reminder_id for reminder_id, _ in reminders])
        messages.append(record)
        summary["reminded"].update(record["task_reminder_ids"])
        delivered.append(_copy_message(record))
    return delivered


def _deliver_due_todo_checks(messages, summary, now):
    items = summary["todos"]
    scheduled = summary["todo_checks"]
    due_ids = [item_id for item_id in scheduled
               if items[item_id]["due_at"] <= now and todo.check_recipient(items[item_id])
               and _todo_can_notify(items[item_id], summary)]
    delivered = []
    for item_id in sorted(due_ids, key=lambda item_id: (items[item_id]["due_at"], item_id)):
        item = items[item_id]
        lines = [f"@{todo.check_recipient(item)} T{item_id} is due."]
        details = (item["title"], item["next_action"])
        if item["source_id"] is None and details[0] == details[1]:
            details = details[:1]
        for detail in details:
            lines.extend("> " + line for line in detail.splitlines())
        check = todo.check_receipt(item, scheduled[item_id])
        record = _write_record("system", "\n".join(lines), "msg", messages, todo_check=check)
        messages.append(record)
        del scheduled[item_id]
        delivered.append(_copy_message(record))
    return delivered


def dispatch_due_reminders(now=None):
    """Append permitted task reminders and to-do checks that are due."""
    now = time.time() if now is None else _reminder_time(now)
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        policy, _ = _guard_state(messages, now)
        if policy["blocked"]:
            return []
        due_by_owner = {agent: [] for agent in AGENTS}
        for reminder_id, event in summary["reminders"].items():
            if (event.get("trigger", "time") != "time" or event["state"] != "scheduled"
                    or reminder_id in summary["reminded"]
                    or event["due_at"] > now):
                continue
            task_id = event["task_id"]
            task = summary["tasks"].get(f"task:{task_id}")
            if task is None or task.get("done"):
                continue
            owner = summary["claims"].get(f"task:{task_id}")
            if owner in due_by_owner:
                due_by_owner[owner].append((reminder_id, event))
        task_notices = _deliver_reminder_batch(messages, summary, due_by_owner)
        return task_notices + _deliver_due_todo_checks(messages, summary, now)


def dispatch_gate_reminders():
    """Deliver exact-commit waits whose revisions are reachable from local main.

    Read eligible events first, check Git outside the shared log lock, then
    revalidate them atomically before posting. This catches up when listeners
    restart after gates finish and never starts Git when no waits can fire.
    """
    with _locked():
        messages = _records()
        summary = _summary(messages)
        candidates = []
        for reminder_id, event in summary["reminders"].items():
            if (event.get("trigger") != "gate" or event["state"] != "scheduled"
                    or reminder_id in summary["reminded"]):
                continue
            task_id = event["task_id"]
            task = summary["tasks"].get(f"task:{task_id}")
            owner = summary["claims"].get(f"task:{task_id}")
            if task is not None and not task.get("done") and owner in AGENTS:
                candidates.append((reminder_id, task_id, event["project"], event["commit"]))
        if not candidates or _guard_state(messages)[0]["blocked"]:
            return []

    heads, checked, failed_projects, reached = {}, {}, set(), set()
    for reminder_id, task_id, project, commit in candidates:
        if project in failed_projects:
            continue
        try:
            if project not in heads:
                heads[project] = gate_reminders.main_head(project)
            head = heads[project]
            key = (project, head, commit)
            if key not in checked:
                checked[key] = gate_reminders.is_ancestor(project, commit, head)
            if checked[key]:
                reached.add((reminder_id, task_id, project, commit))
        except (OSError, ValueError):
            failed_projects.add(project)
    if not reached:
        return []

    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        policy, _ = _guard_state(messages)
        if policy["blocked"]:
            return []
        due_by_owner = {agent: [] for agent in AGENTS}
        for reminder_id, task_id, project, commit in reached:
            event = summary["reminders"].get(reminder_id)
            task_key = f"task:{task_id}"
            task = summary["tasks"].get(task_key)
            owner = summary["claims"].get(task_key)
            if (event is not None and event.get("trigger") == "gate" and event["state"] == "scheduled"
                    and reminder_id not in summary["reminded"] and event["task_id"] == task_id
                    and event["project"] == project and event["commit"] == commit
                    and task is not None and not task.get("done") and owner in due_by_owner):
                due_by_owner[owner].append((reminder_id, event))
        return _deliver_reminder_batch(messages, summary, due_by_owner)


def dispatch_todo_answers():
    """Ask the owner to check a user answer against an open to-do."""
    with _locked():
        if not _summary(_records())["todo_answers"]:
            return []
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        summary = _summary(messages)
        pending = summary["todo_answers"]
        if not pending or _guard_state(messages)[0]["blocked"]:
            return []
        delivered = []
        for cue in sorted(pending.values(), key=lambda cue: (cue["answer"], cue["id"])):
            if not _todo_can_notify(summary["todos"][cue["id"]], summary):
                continue
            text = (f"@{cue['owner']} The user replied in #{cue['answer']}. "
                    f"Is T{cue['id']} still waiting on them?")
            record = _write_record("system", text, "msg", messages, todo_answer=cue)
            messages.append(record)
            delivered.append(_copy_message(record))
        return delivered


def dispatch_todo_waiters():
    """Wake the current owners of open to-dos whose prerequisites have finished."""
    with _locked():
        records = _records()
        summary = _summary(records)
        items = summary["todos"]
        candidates = [item for item in items.values()
                      if item.get("wait_for") and item["result"] is None and _todo_can_notify(item, summary)]
        if not candidates or _guard_state(records)[0]["blocked"]:
            return []

    heads, readiness, reached = {}, {}, []
    for item in candidates:
        wait = item["wait_for"]
        if wait not in readiness:
            kind, project, target = todo.wait_target(wait)
            try:
                if kind == "todo":
                    readiness[wait] = items[target]["result"] is not None
                elif kind == "gate":
                    if project not in heads:
                        heads[project] = gate_reminders.main_head(project)
                    readiness[wait] = gate_reminders.is_ancestor(project, target, heads[project])
                else:
                    readiness[wait] = release_tags.published(project, target)
            except (OSError, ValueError, KeyError):
                readiness[wait] = False
        if readiness[wait]:
            reached.append(item)
    if not reached:
        return []

    with _locked(exclusive=True):
        _repair_tails()
        records = _records()
        summary = _summary(records)
        items = summary["todos"]
        if _guard_state(records)[0]["blocked"]:
            return []
        messages = list(records)
        delivered = []
        for captured in reached:
            item = items[captured["id"]]
            if item["revision"] != captured["revision"] or not _todo_can_notify(item, summary):
                continue
            owner, number = item["owner"], item["id"]
            lines = [f"@{owner} T{number} is ready."]
            lines.extend("> " + line for line in item["title"].splitlines())
            lines.append("> Finished waiting for " + todo.wait_label(item["wait_for"]) + ".")
            event = dict(id=number, revision=item["revision"], actor=owner,
                         changes={"next_actor": owner}, request_id=None)
            record = _write_record("system", "\n".join(lines), "system", messages,
                                   todo_event=event, todo_ready=True)
            messages.append(record)
            delivered.append(_copy_message(record))
        return delivered


def read_messages(after_id=None, limit=None):
    after_id = 0 if after_id is None else _integer(after_id, "after_id")
    if limit is not None:
        _integer(limit, "limit")
        if limit == 0:
            return []
    if after_id and _record_signature is None:
        # A fresh UI/CLI can answer a recent cursor from bounded tails. If the
        # cursor is older than that range, preserve the full unread contract.
        tail_limit = max(1000, limit or 0)
        tail = read_tail(tail_limit)
        if not tail or len(tail) < tail_limit or after_id >= tail[0]["id"] - 1:
            rows = [message for message in tail if message["id"] > after_id]
            return rows if limit is None else rows[:limit]
    with _locked():
        records = _records()
        start = bisect_right(records, after_id, key=lambda message: message["id"])
        messages = records[start:] if limit is None else records[start:start + limit]
    return [_copy_message(message) for message in messages]


def last_id():
    """Latest committed id; sequence metadata can lag the durable log."""
    with _locked():
        messages = _records()
        return messages[-1]["id"] if messages else 0


def read_tail(limit=1000):
    """Read the newest ids without decoding unrelated older history."""
    _integer(limit, "limit")
    if limit == 0:
        return []
    with _locked():
        files = []
        for path in CHAT_DIR.glob("*.jsonl"):
            end = _file_tail(path, 1)
            if end:
                files.append((end[0]["id"], path))
        candidates, seen = [], set()
        for maximum, path in sorted(files, reverse=True):
            if len(candidates) >= limit and maximum < candidates[0]["id"]:
                break
            for message in _file_tail(path, limit):
                if message["id"] in seen:
                    raise ValueError(f"Invalid log record at {path.name}: duplicate message id")
                seen.add(message["id"])
                candidates.append(message)
            candidates.sort(key=lambda message: message["id"])
            candidates = candidates[-limit:]
        return candidates


def search_messages(query, limit=50, before_id=None, *, kind=None):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Search query must contain text")
    _integer(limit, "limit")
    if before_id is not None:
        _integer(before_id, "before_id")
    if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
        raise ValueError("Invalid search kind")
    if limit == 0:
        return []
    needle = query.casefold()
    with _locked():
        matches = []
        for message in reversed(_records()):
            if (before_id is not None and message["id"] >= before_id) or (kind is not None and message["kind"] != kind):
                continue
            if needle in message["text"].casefold():
                matches.append(_copy_message(message))
                if len(matches) == limit:
                    break
        return matches[::-1]


def _presence_directory():
    _prepare()
    directory = DATA_DIR / "presence"
    directory.mkdir(exist_ok=True, mode=0o700)
    return directory


def _lease(path):
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, ValueError, UnicodeError):
        return None
    if (not isinstance(value, dict)
            or not all(type(value.get(key)) in (int, float) and math.isfinite(value[key])
                       for key in ("updated_at", "expires_at"))
            or value["updated_at"] > value["expires_at"]):
        return None
    return value


def _observed_activity(value, now):
    return (value is not None and value.get("state") in ("listening", "working", "idle")
            and isinstance(value.get("note"), str) and len(value["note"]) <= 200
            and type(value.get("since")) in (int, float) and math.isfinite(value["since"])
            and value["since"] <= value["updated_at"] <= now)


def _live_contact(contact, now):
    return contact is not None and contact["updated_at"] <= now < contact["expires_at"]


def set_reachable(agent, *, ttl=90):
    """Refresh contact without saying whether a model turn is running."""
    _agent(agent)
    if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl < 0:
        raise ValueError("Reachability lease must be finite and non-negative")
    directory = _presence_directory()
    with _file_lock(directory / f"{agent}.lock"):
        now = time.time()
        contact = dict(updated_at=now, expires_at=now+ttl)
        _atomic_json(directory / f"{agent}.reach.json", contact)
    return dict(reachable=ttl > 0, expires_at=contact["expires_at"])


def clear_presence(agent):
    """Expire both observations when this agent explicitly leaves."""
    _agent(agent)
    directory = _presence_directory()
    with _file_lock(directory / f"{agent}.lock"):
        now = time.time()
        expired = dict(updated_at=now, expires_at=now)
        _atomic_json(directory / f"{agent}.json", expired)
        _atomic_json(directory / f"{agent}.reach.json", expired)


def set_presence(agent, state, note="", *, ttl=900):
    """Publish work observed by the provider or explicitly reported by an agent."""
    _agent(agent)
    if state not in ("working", "idle"):
        raise ValueError("Presence state must be working or idle")
    if not isinstance(note, str) or len(note) > 200:
        raise ValueError("Presence note must be a string of at most 200 characters")
    if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("Presence lease must be finite and positive")
    directory = _presence_directory()
    path = directory / f"{agent}.json"
    with _file_lock(directory / f"{agent}.lock"):
        now = time.time()
        previous = _lease(path)
        since = now
        if (_observed_activity(previous, now) and now < previous["expires_at"]
                and previous.get("version") == 2 and previous["state"] == state):
            since = previous["since"]
        presence = {"state": state, "since": since, "updated_at": now,
                    "expires_at": now + ttl, "note": note}
        _atomic_json(path, dict(presence, version=2))
        reachable = state == "working" or _live_contact(_lease(directory / f"{agent}.reach.json"), now)
    return dict(presence, reachable=reachable)


def get_presence():
    """Read activity and contact leases independently of the chat log."""
    _prepare()
    result = {}
    now = time.time()
    directory = DATA_DIR / "presence"
    for agent in AGENTS:
        value = _lease(directory / f"{agent}.json")
        observed = _observed_activity(value, now)
        fresh = observed and now < value["expires_at"]
        recorded = observed and value.get("version") == 2 and value["state"] != "listening"
        contact = _lease(directory / f"{agent}.reach.json")
        reachable = _live_contact(contact, now)
        if fresh and recorded and (value["state"] == "working" or reachable):
            result[agent] = {key: value[key] for key in ("state", "since", "updated_at", "expires_at", "note")}
            result[agent]["reachable"] = True
            continue
        # Older mailbox writers called delivery "working". Their leases only establish contact.
        if contact is None and fresh and not recorded:
            reachable = True
        expired_work = recorded and not fresh and value["state"] == "working"
        result[agent] = {"state": "idle" if reachable and not expired_work else "unknown",
                         "since": None, "updated_at": None,
                         "expires_at": None, "note": "", "reachable": reachable}
    return result


def guard_status():
    with _locked():
        return _guard_state(_records())[0]


def active_claims():
    with _locked():
        return _claims(_records())


def _agent(agent):
    if agent not in AGENTS:
        raise ValueError(f"Unknown agent: {agent}")


def _cursor(agent):
    path = CURSOR_DIR / f"{agent}.json"
    try:
        value = json.loads(path.read_text())
        _integer(value["acked"], "acked id")
        return value
    except FileNotFoundError:
        return {"acked": 0}
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f"Invalid acknowledgment file for {agent}: {path}") from error


def _ack(agent):
    return _cursor(agent)["acked"]


def get_ack(agent):
    _agent(agent)
    with _locked():
        return _ack(agent)


def set_ack(agent, msg_id):
    _agent(agent)
    _integer(msg_id, "message id")
    with _locked(exclusive=True):
        messages = _records()
        last = messages[-1]["id"] if messages else 0
        if msg_id > last:
            raise ValueError(f"Cannot acknowledge {msg_id}; last message is {last}")
        cursor = _cursor(agent)
        if msg_id < cursor["acked"]:
            raise ValueError("Acknowledgments cannot move backwards")
        if msg_id == cursor["acked"]:
            return
        stamp = time.time()
        samples = _wake_samples(cursor)
        for sample in samples:
            if sample["id"] <= msg_id and sample["acked_at"] is None:
                sample["acked_at"] = stamp
        _atomic_json(CURSOR_DIR / f"{agent}.json",
                     {"acked": msg_id, "acked_at": stamp, "wakes": samples})


def _timestamp_value(value):
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _wake_samples(cursor):
    samples = cursor.get("wakes")
    if not isinstance(samples, list):
        return []
    result = []
    for sample in samples[-WAKE_SAMPLE_LIMIT:]:
        if (isinstance(sample, dict) and type(sample.get("id")) is int
                and type(sample.get("first_id")) is int and 0 < sample["first_id"] <= sample["id"]
                and _timestamp_value(sample.get("queued_at"))):
            posted_at, acked_at = sample.get("posted_at"), sample.get("acked_at")
            result.append(dict(id=sample["id"], first_id=sample["first_id"], queued_at=sample["queued_at"],
                               posted_at=posted_at if _timestamp_value(posted_at) else None,
                               acked_at=acked_at if _timestamp_value(acked_at) else None))
    return result


def woke(agent, message_id, *, first_id=None, queued_at=None):
    """Record the provider's accepted queue operation for one message or a batch."""
    _agent(agent)
    _integer(message_id, "wake position")
    first = message_id if first_id is None else _integer(first_id, "first wake message")
    stamp = time.time() if queued_at is None else queued_at
    if not 0 < first <= message_id or not _timestamp_value(stamp):
        raise ValueError("Invalid wake range or time")
    with _locked(exclusive=True):
        records = _records()
        end = bisect_right(records, message_id, key=lambda message: message["id"])
        message = records[end-1] if end else None
        if (message is None or message["id"] != message_id or message["kind"] != "msg"
                or message["sender"] == agent):
            raise ValueError("Wake position must refer to an incoming message")
        cursor = _cursor(agent)
        samples = _wake_samples(cursor)
        previous = next((sample for sample in samples if sample["id"] == message_id), None)
        if previous is None:
            samples.append(dict(id=message_id, first_id=first, queued_at=stamp,
                                posted_at=_timestamp(message, None),
                                acked_at=cursor.get("acked_at") if cursor["acked"] == message_id else None))
        else:
            previous["first_id"] = min(previous["first_id"], first)
            previous["queued_at"] = min(previous["queued_at"], stamp)
        bounded = samples[-WAKE_SAMPLE_LIMIT:]
        if bounded != cursor.get("wakes"):
            _atomic_json(CURSOR_DIR / f"{agent}.json", dict(cursor, wakes=bounded))


def _elapsed(start, end):
    if _timestamp_value(start) and _timestamp_value(end) and end >= start:
        return round(end-start, 3)
    return None


def wake_ranges(agent):
    """Return the retained inclusive (first_id, last_id) queue receipts for an agent."""
    _agent(agent)
    with _locked():
        samples = _wake_samples(_cursor(agent))
    return tuple((sample["first_id"], sample["id"]) for sample in samples)


def wake_timing(agent):
    """Return bounded diagnostic timing from log arrival to queue and acknowledgment."""
    _agent(agent)
    with _locked():
        cursor = _cursor(agent)
        samples = _wake_samples(cursor)
        records = _records()
        start = bisect_right(records, cursor["acked"], key=lambda message: message["id"])
        oldest = next((row for row in records[start:] if row["kind"] == "msg" and row["sender"] == USER), None)
    waiting = None
    if oldest is not None:
        id_ = oldest["id"]
        waiting = dict(id=id_, age=_elapsed(_timestamp(oldest, None), time.time()),
                       queued=any(sample["first_id"] <= id_ <= sample["id"] for sample in samples))
    recent = [dict(id=sample["id"], wake_s=_elapsed(sample["posted_at"], sample["queued_at"]),
                   ack_s=_elapsed(sample["posted_at"], sample["acked_at"])) for sample in samples]
    return dict(oldest_unacked=waiting, last=recent[-1] if recent else None, recent=recent)


def _inotify(directories=None, *, event_mask=0x00000FCE):
    """None means inotify is unavailable; use the portable polling fallback."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add = libc.inotify_add_watch
        add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add.restype = ctypes.c_int
        fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            return None
        # File content, close, creation, deletion and rename events; directory too.
        for directory in ((CHAT_DIR,) if directories is None else directories):
            if add(fd, os.fsencode(directory), event_mask) < 0:
                os.close(fd)
                return None
        return fd
    except (AttributeError, OSError):
        return None


def _signature():
    try:
        return tuple((p.name, s.st_ino, s.st_size, s.st_mtime_ns)
                     for p in sorted(CHAT_DIR.glob("*.jsonl")) for s in [p.stat()])
    except FileNotFoundError:
        return None


def _watch_lost(events):
    position = 0
    while position + 16 <= len(events):
        _, mask, _, length = struct.unpack_from("iIII", events, position)
        if mask & (0x400 | 0x800 | 0x8000):  # DELETE_SELF, MOVE_SELF, IGNORED
            return True
        position += 16 + length
    return False


def wait_for_change(after_id, timeout=25, *, wake_when=None):
    _integer(after_id, "after_id")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite non-negative number")
    _prepare()
    def watching():
        if wake_when is None:
            return _inotify()
        # The control files are replaced in the data directory. Exclude
        # CLOSE_WRITE from read locks, which would wake this same loop.
        return _inotify((CHAT_DIR, DATA_DIR), event_mask=0x00000FC6)

    def wake_ready():
        return (wake_when is not None and wake_when()) or bool(read_messages(after_id, limit=1))

    # Install watch BEFORE checking for unread records; no read/watch gap.
    fd = watching()
    deadline = time.monotonic() + timeout
    try:
        # In polling mode, snapshot before reading for the same race protection.
        signature = _signature() if fd is None else None
        if wake_ready():
            return True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return wake_ready()
            if fd is None:
                time.sleep(min(0.5, remaining))
                current = _signature()
                if current == signature and wake_when is None:
                    continue
                signature = current
            else:
                try:
                    ready, _, _ = select.select([fd], [], [], remaining)
                except InterruptedError:
                    continue
                if not ready:
                    return wake_ready()
                lost = False
                try:
                    while True:
                        events = os.read(fd, 65536)
                        if not events:
                            lost = True
                            break
                        lost = _watch_lost(events) or lost
                except BlockingIOError:
                    pass
                if lost:
                    os.close(fd)
                    fd = watching()
                    signature = _signature() if fd is None else None
            if wake_ready():
                return True
    finally:
        if fd is not None:
            os.close(fd)
