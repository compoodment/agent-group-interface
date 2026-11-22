"""An explicit one-shot test of Claude's selected channel and its reply path."""

import json
import time
import uuid

from agi import membership, store


TIMEOUT = 60


def _path():
    return store.DATA_DIR / ".claude-delivery-test.json"


def _read():
    try:
        record = json.loads(_path().read_text())
    except FileNotFoundError:
        return None
    names = {"id", "session", "revision", "expires_at", "sent_at", "confirmed_at"}
    if (not isinstance(record, dict) or record.keys() != names
            or not store._finite_nonnegative_number(record["expires_at"])
            or any(record[name] is not None and not store._finite_nonnegative_number(record[name])
                   for name in ("sent_at", "confirmed_at"))):
        raise ValueError("Invalid Claude delivery test.")
    store._request_id(record["id"])
    store._request_id(record["revision"])
    membership._session(record["session"])
    return record


def _state(record, now):
    if record is None:
        return {"state": "idle"}
    if not membership.can_deliver("claude", record["session"], revision=record["revision"]):
        label = "cancelled"
    elif record["confirmed_at"] is not None:
        label = "confirmed"
    elif now >= record["expires_at"]:
        label = "expired"
    else:
        label = "sent" if record["sent_at"] is not None else "queued"
    return dict(record, state=label)


def status():
    return _state(_read(), time.time())


def pending_for(session):
    current = status()
    return current["state"] == "queued" and current["session"] == session


def request():
    """Queue a ping only when a person asks to test the selected live session."""
    with store._locked(exclusive=True):
        current = membership.status("claude")
        if not current["joined"] or not current["reachable"]:
            raise ValueError("Join the Claude session before testing its delivery.")
        if status()["state"] in ("queued", "sent"):
            raise ValueError("A Claude delivery test is already pending.")
        record = dict(id=uuid.uuid4().hex, session=current["session"], revision=current["revision"],
                      expires_at=time.time() + TIMEOUT, sent_at=None, confirmed_at=None)
        store._atomic_json(_path(), record)
        return _state(record, time.time())


def take_pending(session, revision):
    """Reserve one send for the selected channel. Expired requests stay quiet."""
    with store._locked(exclusive=True):
        record = _read()
        now = time.time()
        if (_state(record, now)["state"] != "queued"
                or (record["session"], record["revision"]) != (session, revision)):
            return None
        record["sent_at"] = now
        store._atomic_json(_path(), record)
        return record["id"]


def confirm(session, probe_id):
    """Accept a reply to the current probe without changing the conversation ack."""
    store._request_id(probe_id)
    with store._locked(exclusive=True):
        record = _read()
        now = time.time()
        current = _state(record, now)
        if current["state"] not in ("sent", "confirmed") or (record["session"], record["id"]) != (session, probe_id):
            raise ValueError("There is no matching delivery test for this Claude session.")
        if record["confirmed_at"] is None:
            record["confirmed_at"] = now
            store._atomic_json(_path(), record)
        return _state(record, now)
