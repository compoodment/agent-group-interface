"""To-do event validation and projection, shared by the reader and writer."""

import math
import re

from agi import gate_reminders


PARTICIPANTS = ("user", "claude", "codex")
TEXT_LIMITS = {"project": 80, "title": 120, "next_action": 500, "reference": 1000, "result": 1000}
INITIAL_FIELDS = frozenset(("project", "title", "owner", "next_action", "next_actor", "due_at", "reference"))
OPTIONAL_FIELDS = {"remind_next_actor"}
FIELDS = INITIAL_FIELDS | OPTIONAL_FIELDS | {"wait_for", "follow_request"}
RELEASE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def wait_target(value):
    """Decode a stored prerequisite into its checked kind, project and target."""
    if not isinstance(value, str):
        raise ValueError("Invalid to-do prerequisite")
    parts = value.split(":")
    if len(parts) == 2 and parts[0] == "todo" and re.fullmatch(r"[1-9][0-9]{0,11}", parts[1]):
        return "todo", None, int(parts[1])
    if len(parts) == 3:
        kind, project, target = parts
        if kind == "gate":
            gate_reminders.validate_target_format(project, target)
            return kind, project, target
        if kind == "release" and gate_reminders.project_id(project) == project and RELEASE_TAG.fullmatch(target):
            return kind, project, target
    raise ValueError("Prerequisite must name a to-do, a full main commit or a public release tag")


def wait_label(value):
    kind, project, target = wait_target(value)
    if kind == "todo":
        return f"T{target} to finish"
    if kind == "gate":
        return f"{gate_reminders.display_name(project)} main to include {target}"
    return f"{gate_reminders.display_name(project)} to publish {target}"


def positive_id(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _valid_deadline(value):
    return type(value) in (float, int) and 0 <= value <= 253402300799 and math.isfinite(value)


def validate_request_id(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value)):
        raise ValueError("To-do request ID must contain 32 lowercase hexadecimal characters")


def validate_event(event):
    if not isinstance(event, dict) or set(event) != {"actor", "id", "revision", "changes", "request_id"}:
        raise ValueError("Invalid to-do event fields")
    if event["actor"] not in PARTICIPANTS:
        raise ValueError("To-do actor must be user, claude or codex")
    validate_request_id(event["request_id"])
    changes = event["changes"]
    if not isinstance(changes, dict) or not changes:
        raise ValueError("A to-do event requires changes")
    if event["id"] is None:
        required = INITIAL_FIELDS | {"source_id"}
        if event["revision"] is not None or not required <= changes.keys() <= required | OPTIONAL_FIELDS | {"follow_request"}:
            raise ValueError("Adding a to-do requires its source and all initial fields")
        if changes["source_id"] is None:
            if (event["actor"], changes["owner"], changes["next_actor"]) != ("user", "user", "user"):
                raise ValueError("Only the user can add a personal to-do")
            if changes.get("follow_request"):
                raise ValueError("A following to-do needs a source request")
        else:
            positive_id(changes["source_id"], "Source ID")
    else:
        positive_id(event["id"], "To-do ID")
        positive_id(event["revision"], "To-do revision")
        if set(changes) != {"result"} and not changes.keys() <= INITIAL_FIELDS | OPTIONAL_FIELDS:
            raise ValueError("Unknown or uneditable to-do field")
    for field, value in changes.items():
        if field in TEXT_LIMITS and not (field == "reference" and value is None):
            if not isinstance(value, str) or not value.strip() or len(value) > TEXT_LIMITS[field]:
                raise ValueError(f"{field} must contain 1–{TEXT_LIMITS[field]} characters")
        if field == "owner" and value not in PARTICIPANTS:
            raise ValueError("To-do owner must be user, claude or codex")
        if field == "next_actor" and value not in (*PARTICIPANTS, None):
            raise ValueError("To-do next actor must be user, claude or codex, or empty")
        if field == "due_at" and value is not None and not _valid_deadline(value):
            raise ValueError("To-do deadline must be Unix seconds within the supported date range")
        if field == "remind_next_actor" and type(value) is not bool:
            raise ValueError("Routing the deadline to the next actor must be true or false")
        if field == "follow_request" and value is not True:
            raise ValueError("A request link must be selected when the to-do is created")


def validate_check(check, message_id):
    fields = {"id", "schedule_revision", "due_at", "owner"}
    if (not isinstance(check, dict) or not fields <= check.keys() <= fields | {"recipient"}
            or not _valid_deadline(check["due_at"]) or check["owner"] not in PARTICIPANTS
            or check.get("recipient", check["owner"]) not in PARTICIPANTS):
        raise ValueError("Invalid timed to-do check")
    positive_id(check["id"], "To-do ID")
    positive_id(check["schedule_revision"], "To-do schedule revision")
    if not check["id"] <= check["schedule_revision"] < message_id:
        raise ValueError("To-do schedule must precede its check")


def check_recipient(item):
    return item["next_actor"] if item.get("remind_next_actor") else item["owner"]


def check_receipt(item, schedule_revision):
    receipt = dict(id=item["id"], schedule_revision=schedule_revision, due_at=item["due_at"], owner=item["owner"])
    recipient = check_recipient(item)
    if recipient != item["owner"]:
        receipt["recipient"] = recipient
    return receipt


def validate_answer_target(target, answer_id):
    if not isinstance(target, dict) or target.keys() != {"id", "revision", "owner"}:
        raise ValueError("Invalid to-do answer target")
    positive_id(target["id"], "To-do ID")
    positive_id(target["revision"], "To-do revision")
    if not target["id"] <= target["revision"] < answer_id or target["owner"] not in ("claude", "codex"):
        raise ValueError("A to-do answer needs an earlier revision and an agent owner")


def validate_next(event):
    if not isinstance(event, dict) or set(event) != {"actor", "owner", "todo_id", "request_id"}:
        raise ValueError("Invalid next to-do record")
    actor, owner = event["actor"], event["owner"]
    if owner not in PARTICIPANTS or actor not in ("user", owner):
        raise ValueError("Only the owner or the user can change the next to-do")
    if event["todo_id"] is not None:
        positive_id(event["todo_id"], "To-do ID")
    validate_request_id(event["request_id"])


def public_item(item, next_todos):
    is_next = item["result"] is None and next_todos.get(item["owner"]) == item["id"]
    return dict(item, project=gate_reminders.display_name(item["project"]), is_next=is_next)


def apply_event(items, record):
    event = record["todo_event"]
    item_id = event["id"]
    changes = event["changes"]
    if item_id is None:
        item_id = record["id"]
        item = dict(changes, id=item_id, created_at=record["ts"], result=None)
    else:
        prior = items.get(item_id)
        if prior is None or prior["revision"] != event["revision"] or prior["result"] is not None:
            raise ValueError(f"Invalid to-do revision for #{item_id}")
        if record.get("todo_ready") and (
                not prior.get("wait_for") or changes["next_actor"] != prior["owner"]):
            raise ValueError(f"Invalid to-do readiness for #{item_id}")
        item = dict(prior, **changes)
        if item["result"] is not None:
            item["next_actor"] = None
    if record.get("todo_wait") is not None:
        item["wait_for"] = record["todo_wait"]
    elif "todo_wait" in record or record.get("todo_ready") or item["result"] is not None:
        item.pop("wait_for", None)
    item.update(revision=record["id"], updated_at=record["ts"])
    items[item_id] = item
    return item
