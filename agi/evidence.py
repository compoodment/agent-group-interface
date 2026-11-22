"""Suggest evidence for an active task from local receipts."""

from datetime import datetime
from pathlib import Path

from agi import gate_reminders, gates, skills, store


def _time(value):
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() is not None:
            return timestamp
    except (TypeError, ValueError):
        pass
    raise ValueError("Evidence requires a timestamp with a timezone")


def _skill_names(agent, since):
    try:
        receipts = skills.recent(agent)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("Could not read skill receipts") from error
    names = set()
    for receipt in receipts:
        name = receipt.get("name")
        if not isinstance(name, str) or not skills.NAME.fullmatch(name):
            raise ValueError("Invalid skill receipt name")
        if _time(receipt.get("read_at")) >= since:
            names.add(name)
    return sorted(names)


def _gate_check(project, commit):
    project = gate_reminders.project_id(project)
    gate_reminders.validate_target_format(project, commit)
    directory = gate_reminders._repository(project) / ".git" / "gate-status"
    for path in sorted(directory.glob("*.json")):
        try:
            receipt = gates._read_status(path)
        except (FileNotFoundError, ValueError, RecursionError):
            continue
        if (isinstance(receipt, dict) and receipt.get("version") == 1
                and receipt.get("run_id") == path.stem and receipt.get("state") == "integrated"
                and receipt.get("candidate") == commit):
            return f"{gate_reminders.display_name(project)} gate integrated {commit} (run {path.stem})"
    raise ValueError("No local integration receipt for that commit")


def _report_check(report):
    try:
        path = Path(report).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise ValueError("Choose an existing local report file") from error
    if not path.is_file() or not str(path).isprintable():
        raise ValueError("Choose an existing local report file with a printable path")
    return f"Report {path}"


def suggest(agent, task_id, *, gate_commit=None, report=None, project="AGI"):
    """Read suggestions for the current owner's claim without changing the task."""
    store._agent(agent)
    if gate_commit is not None and report is not None:
        raise ValueError("Choose one gate commit or one report")
    claim = store.task_claim(task_id)
    if claim is None or claim["owner"] != agent:
        raise store.ClaimConflict(f"No active claim for {agent} on task #{task_id}")
    names = _skill_names(agent, _time(claim["since"]))
    check = None
    if gate_commit is not None:
        check = _gate_check(project, gate_commit)
    elif report is not None:
        check = _report_check(report)
    return {"claim_id": claim["claim_id"], "skills": names, "check": check}
