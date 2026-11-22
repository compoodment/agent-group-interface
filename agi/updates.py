"""Installed release notes and an explicit, local receipt for each reader."""

from datetime import date
import json
import re

from agi import __version__, store


_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+"
                      r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
                      r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_BLOCK = re.compile(r"^ {0,3}(?:(?P<fence>`{3,}|~{3,})(?P<tail>[^\n]*)"
                    r"|##[ \t]+(?P<heading>[^\n]*))", re.MULTILINE)


def _sections(changelog):
    """Yield level-two sections, ignoring heading examples inside code blocks."""
    fence = None
    headings = []
    for block in _BLOCK.finditer(changelog):
        marker = block["fence"]
        if fence is not None:
            if marker and marker.startswith(fence) and not block["tail"].strip():
                fence = None
        elif marker:
            fence = marker
        else:
            headings.append(block)
    ends = [block.start() for block in headings[1:]] + [len(changelog)]
    for block, end in zip(headings, ends):
        yield block["heading"].split(), changelog[block.end():end].strip("\n")


def current_release():
    """Read the one dated section matching the installed VERSION."""
    version = __version__
    if not _VERSION.fullmatch(version):
        return None
    try:
        changelog = (store.CHECKOUT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeError):
        return None

    matches = [(fields, markdown) for fields, markdown in _sections(changelog) if fields[:1] == [version]]
    if len(matches) != 1:
        return None

    fields, markdown = matches[0]
    if len(fields) != 3 or fields[1] != "—" or not _DATE.fullmatch(fields[2]) or not markdown.strip():
        return None
    try:
        released = date.fromisoformat(fields[2])
    except ValueError:
        return None
    return {"version": version, "date": released.isoformat(), "markdown": markdown}


def _validate_reader(reader):
    if reader not in (store.USER, *store.AGENTS):
        raise ValueError("Unknown release notes reader")


def _receipt_path(reader, version):
    return store.DATA_DIR / "updates" / f"{reader}-{version}.json"


def _has_receipt(path, version):
    try:
        return json.loads(path.read_text(encoding="utf-8")) == {"version": version}
    except (FileNotFoundError, ValueError):
        return False


def unread_release(reader):
    """Return the current entry if it is still unread for this reader."""
    _validate_reader(reader)
    release = current_release()
    if release is None:
        return None
    version = release["version"]
    return None if _has_receipt(_receipt_path(reader, version), version) else release


def acknowledge_release(reader, version):
    """Record a receipt for the version the reader received, independently of chat acks."""
    _validate_reader(reader)
    if version != __version__:
        raise ValueError("Release notes acknowledgment must match the installed version")
    if current_release() is None:
        raise ValueError("The installed version has no valid changelog entry")

    store._prepare()
    receipt = _receipt_path(reader, version)
    directory = receipt.parent
    directory.mkdir(exist_ok=True, mode=0o700)
    store._sync_directory(store.DATA_DIR)
    with store._file_lock(directory / f"{reader}.lock"):
        if _has_receipt(receipt, version):
            # A previous write may have published the file before a failed directory sync.
            store._sync_directory(directory)
        else:
            store._atomic_json(receipt, {"version": version})
    return version
