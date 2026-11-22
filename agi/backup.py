"""Export and recover a verified copy of the conversation and saved UI state."""

import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from agi import __version__, store


DIRECTORIES = ("admin-mentions", "attachments", "chat", "cursors", "recovery", "skills", "ui", "updates")
FILES = ("autonomy.json", "membership.json", "policy.json", "seq")
TRANSIENT = {Path("ui/sounds.log")}


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _collect(source):
    files = {}

    def visit(path):
        relative = path.relative_to(source)
        if path.name.startswith(".") or path.suffix == ".lock" or relative in TRANSIENT:
            return
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            for child in path.iterdir():
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            files[relative] = _identity(info)
        else:
            raise ValueError(f"Cannot back up a link or special file: {relative}")

    for name in DIRECTORIES + FILES:
        entry = source / name
        if entry.exists() or entry.is_symlink():
            visit(entry)
    return files


def _snapshot(source, archive):
    # The chat lock fixes the message log, attachments and agent read positions.
    # UI drafts and markers are atomic files with separate writers. Compare
    # their identities around the copy so a moving snapshot is never published.
    try:
        with store._locked(exclusive=True):
            files = _collect(source)
            for relative, identity in sorted(files.items()):
                fd = os.open(source / relative, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as data:
                    if _identity(os.fstat(data.fileno())) != identity:
                        raise ValueError("Conversation changed during backup. Try again when it is quiet.")
                    with archive.open(f"conversation/{relative.as_posix()}", "w", force_zip64=True) as target:
                        shutil.copyfileobj(data, target)
                    if _identity(os.fstat(data.fileno())) != identity:
                        raise ValueError("Conversation changed during backup. Try again when it is quiet.")
            if _collect(source) != files:
                raise ValueError("Conversation changed during backup. Try again when it is quiet.")
    except FileNotFoundError as error:
        raise ValueError("Conversation changed during backup. Try again when it is quiet.") from error
    return {path.as_posix(): files[path][3] for path in sorted(files)}


def create(destination=None):
    source = store.DATA_DIR.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("The AGI conversation directory does not exist.")
    now = datetime.now(timezone.utc)
    if destination is None:
        destination = source.with_name(source.name + "-backups") / f"agi-{now:%Y%m%dT%H%M%S.%fZ}.zip"
    path = Path(destination).expanduser()
    parent = path.parent.resolve()
    if parent.is_relative_to(source):
        raise ValueError("Choose a backup path outside the conversation directory.")
    destination = parent / path.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Backup already exists: {destination}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    fd, name = tempfile.mkstemp(prefix=".agi-backup-", dir=parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w+b") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                files = _snapshot(source, archive)
                record = {"format": 1, "app_version": __version__, "created_at": now.isoformat(), "files": files}
                archive.writestr("backup.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            with zipfile.ZipFile(stream) as archive:
                if archive.testzip() is not None:
                    raise ValueError("The backup could not be verified.")
        # Publish without overwriting an older or concurrently created backup.
        os.link(temporary, destination)
        store._sync_directory(parent)
        store._sync_directory(parent.parent)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _restore_file(archive, name, mtime, root):
    path = PurePosixPath(name)
    if (str(path) != name or not path.parts or ".." in path.parts
            or any(part.startswith(".") or part.endswith(".lock") for part in path.parts)
            or Path(path) in TRANSIENT or type(mtime) is not int
            or not (name in FILES or len(path.parts) > 1 and path.parts[0] in DIRECTORIES)):
        raise ValueError("The backup contains an unexpected conversation file.")
    target = root.joinpath(*path.parts)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output, archive.open(f"conversation/{name}") as data:
        shutil.copyfileobj(data, output)
        output.flush()
        os.utime(output.fileno(), ns=(mtime, mtime))
        os.fsync(output.fileno())


def _publish_directory(staged, destination):
    # The Linux no-replace rename also refuses an existing empty directory.
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise ValueError("Restoring an AGI backup requires Linux support for atomic folder creation.") from error
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    at_fdcwd, rename_noreplace = -100, 1
    if rename(at_fdcwd, os.fsencode(staged), at_fdcwd, os.fsencode(destination), rename_noreplace):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def restore(backup, destination):
    path = Path(destination).expanduser()
    destination = path.parent.resolve(strict=True) / path.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Restore folder already exists: {destination}")
    with tempfile.TemporaryDirectory(prefix=".agi-restore-", dir=destination.parent) as scratch:
        root = Path(scratch) / "conversation"
        root.mkdir(mode=0o700)
        try:
            with zipfile.ZipFile(Path(backup).expanduser()) as archive:
                if archive.getinfo("backup.json").file_size > 16 * 1024 * 1024:
                    raise ValueError("The backup manifest is too large.")
                record = json.loads(archive.read("backup.json"))
                if not isinstance(record, dict) or record.get("format") != 1:
                    raise ValueError("This backup format is unsupported.")
                files = record.get("files")
                names = archive.namelist()
                if (not isinstance(files, dict) or len(names) != len(files) + 1
                        or set(names) != {"backup.json", *(f"conversation/{name}" for name in files)}):
                    raise ValueError("The backup does not match its manifest.")
                for name, mtime in files.items():
                    _restore_file(archive, name, mtime, root)
        except (KeyError, OverflowError, RuntimeError, zipfile.BadZipFile) as error:
            raise ValueError("The AGI backup is unreadable.") from error
        for directory, _, _ in os.walk(root, topdown=False):
            store._sync_directory(directory)
        _publish_directory(root, destination)
        store._sync_directory(destination.parent)
    return destination
