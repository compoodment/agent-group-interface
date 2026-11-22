"""Read installed skills and keep the latest local receipt per agent and skill."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from agi import store


NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")


def _source(agent, name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Give an installed skill name, without a path")
    home = Path.home()
    provider = home / f".{agent}" / "skills"
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config").expanduser()
    shared = Path(os.environ.get("AGI_SKILLS_DIR") or config / "agi" / "skills").expanduser()
    roots = (provider, provider / ".system", shared)
    for root in roots:
        file = root / name / "SKILL.md"
        if file.is_file():
            return file.resolve(strict=True)
    raise ValueError(f"No installed skill named {name}")


def _directory(agent):
    if agent not in store.AGENTS:
        raise ValueError("Skill reader must be claude or codex")
    return store.DATA_DIR / "skills" / agent


def read(agent, name):
    """Publish a receipt for the same bytes returned to the operator."""
    directory = _directory(agent)
    source = _source(agent, name)
    store._prepare()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    store._sync_directory(store.DATA_DIR)
    store._sync_directory(directory.parent)
    with store._file_lock(directory / ".lock"):
        data = source.read_bytes()
        data.decode("utf-8")
        receipt = {"name": name, "path": str(source), "sha256": hashlib.sha256(data).hexdigest(),
                   "read_at": datetime.now(timezone.utc).isoformat()}
        store._atomic_json(directory / f"{name}.json", receipt)
    return data, receipt


def recent(agent):
    """Read current receipts without creating state or changing the inbox."""
    return sorted((json.loads(file.read_text(encoding="utf-8")) for file in _directory(agent).glob("*.json")),
                  key=lambda receipt: (receipt["read_at"], receipt["name"]), reverse=True)
