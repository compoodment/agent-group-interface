"""One selected, explicitly joined session per agent in a conversation."""

import json
import math
import re
import shlex
import time
import uuid

from agi import store


SESSION_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}")
EMPTY = {"session": None, "joined": False, "pending": None, "revision": None}


def _session(value):
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise ValueError("Invalid AGI session ID.")
    return value


def _path():
    return store.DATA_DIR / "membership.json"


def _read():
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or not isinstance(data.get("agents"), dict) or set(data["agents"]) - set(store.AGENTS)):
        raise ValueError("Invalid AGI session membership.")
    for entry in data["agents"].values():
        if (not isinstance(entry, dict) or entry.keys() != EMPTY.keys()
                or type(entry["joined"]) is not bool or (entry["joined"] and entry["session"] is None)
                or (entry["pending"] is not None and entry["pending"] == entry["session"])):
            raise ValueError("Invalid AGI session membership entry.")
        for key in ("session", "pending"):
            if entry[key] is not None:
                _session(entry[key])
        store._request_id(entry["revision"])
    return data["agents"]


def _lease_path(agent, session):
    return store.DATA_DIR / "presence" / f"{agent}-session-{session}.json"


def _reachable(agent, session):
    return session is not None and store._live_contact(store._lease(_lease_path(agent, session)), time.time())


def _status(agent, entry):
    contact = store._lease(_lease_path(agent, entry["session"])) if entry["session"] is not None else None
    return dict(entry, reachable=store._live_contact(contact, time.time()),
                last_contact=contact["updated_at"] if contact is not None else None,
                pending_reachable=_reachable(agent, entry["pending"]))


def status(agent):
    store._agent(agent)
    return _status(agent, _read().get(agent, EMPTY))


def resume_command(agent):
    """Print the provider's command for the exact saved conversation, when known."""
    from agi import connect

    session = status(agent)["session"]
    try:
        if session is None or str(uuid.UUID(session)) != session:
            return None
    except ValueError:
        return None
    command = connect.find(agent)
    if command is None:
        return None
    if agent == "claude":
        return shlex.join([command, "--resume", session,
                           "--dangerously-load-development-channels", "server:agi"])
    return shlex.join([command, "resume", session])


def observe(agent, session, *, ttl=90):
    store._agent(agent)
    _session(session)
    if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("Session presence needs a finite positive lifetime.")
    store._presence_directory()
    now = time.time()
    store._atomic_json(_lease_path(agent, session), dict(updated_at=now, expires_at=now + ttl))


def _change(agents, agent, old, **fields):
    entry = dict(old, **fields)
    if (old["session"], old["joined"]) != (entry["session"], entry["joined"]):
        entry["revision"] = uuid.uuid4().hex
    if entry != old:
        agents[agent] = entry
        store._atomic_json(_path(), dict(version=1, agents=agents))
    return _status(agent, entry)


def request_join(agent, session):
    return _request_join(agent, session)[1]


def _request_join(agent, session):
    store._agent(agent)
    _session(session)
    with store._locked(exclusive=True):
        agents = _read()
        entry = agents.get(agent, EMPTY)
        previous = _status(agent, entry)
        if not _reachable(agent, session):
            raise ValueError(f"This {agent} session has no live connection to AGI.")
        if entry["session"] in (None, session):
            return previous, _change(agents, agent, entry, session=session, joined=True)
        pending = entry["pending"]
        if pending not in (None, session) and _reachable(agent, pending):
            raise ValueError(f"Another {agent} session is already waiting to join.")
        return previous, _change(agents, agent, entry, pending=session)


def resume(agent, session=None):
    return _resume(agent, session)[1]


def _resume(agent, session=None, *, expected_session=None):
    store._agent(agent)
    with store._locked(exclusive=True):
        agents = _read()
        entry = agents.get(agent, EMPTY)
        previous = _status(agent, entry)
        if session is None:
            selected = entry["session"]
            pending = entry["pending"]
        else:
            selected = _session(session)
            if entry["pending"] != selected:
                raise ValueError(f"The {agent} join request has changed.")
            pending = None
        if expected_session is not None and selected != expected_session:
            raise ValueError(f"The selected {agent} session has changed.")
        if not _reachable(agent, selected):
            raise ValueError(f"No selected {agent} session is connected. Join from that session.")
        return previous, _change(agents, agent, entry, session=selected, joined=True, pending=pending)


def decline(agent, session):
    store._agent(agent)
    _session(session)
    with store._locked(exclusive=True):
        agents = _read()
        entry = agents.get(agent, EMPTY)
        if entry["pending"] != session:
            raise ValueError(f"The {agent} join request has changed.")
        return _change(agents, agent, entry, pending=None)


def leave(agent, session=None):
    store._agent(agent)
    with store._locked(exclusive=True):
        agents = _read()
        entry = agents.get(agent, EMPTY)
        if session is not None and _session(session) != entry["session"]:
            raise ValueError(f"This {agent} session is not the joined one.")
        return _change(agents, agent, entry, joined=False, pending=None)


def can_deliver(agent, session, *, revision=None):
    store._agent(agent)
    _session(session)
    entry = _read().get(agent, EMPTY)
    return (entry["joined"] and entry["session"] == session
            and (revision is None or entry["revision"] == revision))


def _undo_failed_join(agent, attempted, previous):
    """Restore the old selection if nothing else changed it during startup."""
    before = {key: previous[key] for key in EMPTY}
    failed = {key: attempted[key] for key in EMPTY}
    if before == failed:
        return
    with store._locked(exclusive=True):
        agents = _read()
        if agents.get(agent, EMPTY) != failed:
            return
        if before == EMPTY:
            del agents[agent]
            store._atomic_json(_path(), dict(version=1, agents=agents))
        else:
            _change(agents, agent, failed, session=before["session"], joined=before["joined"],
                    pending=before["pending"])
