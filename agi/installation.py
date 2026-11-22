"""Install, update and remove managed AGI code for one user."""

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress

from agi import __version__, release_assets, store


LAUNCHERS = ("agi", "agi-mcp")
RECEIPT = ".agi-install.json"
SOURCE = Path(__file__).resolve().parents[1]


def _default_prefix():
    xdg = Path(os.environ.get("XDG_DATA_HOME", ""))
    return (xdg if xdg.is_absolute() else Path.home() / ".local/share") / "agi"


def _receipt(root):
    if (root / RECEIPT).is_symlink() or not (root / RECEIPT).is_file():
        raise ValueError(f"Missing AGI install record in {root}.")
    data = json.loads((root / RECEIPT).read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != 1
            or data.get("version") != root.name or not isinstance(data.get("bin_dir"), str)
            or not Path(data["bin_dir"]).is_absolute()):
        raise ValueError(f"Invalid AGI install record in {root}.")
    return data


def _read_receipt(root):
    return Path(_receipt(root)["bin_dir"])


def _current(prefix):
    pointer = prefix / "current"
    if not pointer.is_symlink():
        if pointer.exists():
            raise ValueError(f"{pointer} already exists. Choose another install location.")
        return None
    target = Path(pointer.readlink())
    if target.parts != ("versions", target.name) or not release_assets.VERSION.fullmatch(target.name):
        raise ValueError(f"{pointer} points outside the managed AGI versions.")
    root = prefix / target
    if root.is_symlink():
        raise ValueError(f"{root} must be an AGI version directory.")
    _read_receipt(root)
    return root


def _check_placement(prefix, bin_dir):
    conversation = store.DATA_DIR.resolve()
    if (prefix.is_relative_to(conversation) or conversation.is_relative_to(prefix)
            or bin_dir.is_relative_to(prefix) or bin_dir.is_relative_to(conversation)):
        raise ValueError("Choose separate locations for installed code, commands and conversation data.")
    if (prefix / "versions").is_symlink():
        raise ValueError(f"{prefix / 'versions'} must be a directory inside the install location.")
    for command in LAUNCHERS:
        path = bin_dir / command
        target = prefix / "current" / "bin" / command
        if path.is_symlink() and path.readlink() == target:
            continue
        if path.exists() or path.is_symlink():
            raise ValueError(f"{path} already exists. Choose another command directory.")


def _copy_runtime(source, target):
    def ignore(parent, names):
        skipped = {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}
        for name in set(names) - skipped:
            if (Path(parent) / name).is_symlink():
                raise ValueError(f"Unexpected symbolic link in the AGI runtime: {name}")
        return skipped

    for directory in ("agi", "docs"):
        if (source / directory).is_symlink():
            raise ValueError(f"Unexpected symbolic link in the AGI release: {directory}")
        shutil.copytree(source / directory, target / directory, ignore=ignore)
    (target / "bin").mkdir()
    for name in ("VERSION", "README.md", "CHANGELOG.md", *(f"bin/{command}" for command in LAUNCHERS)):
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"AGI release is missing {name}.")
        shutil.copy2(path, target / name)
    if (source / "LICENSE").is_file() and not (source / "LICENSE").is_symlink():
        shutil.copy2(source / "LICENSE", target / "LICENSE")


def _check_runtime(root, version, scratch):
    env = {**os.environ, "AGI_DATA": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    try:
        version_check = subprocess.run([root / "bin/agi", "--version"], check=False,
                                       capture_output=True, text=True, timeout=15, env=env)
        mcp_check = subprocess.run([root / "bin/agi-mcp", "--help"], check=False,
                                   capture_output=True, text=True, timeout=15, env=env)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"The staged AGI {version} took too long to start.") from error
    if (version_check.returncode != 0 or version_check.stdout.strip() != f"AGI {version}"
            or mcp_check.returncode != 0):
        raise ValueError(f"The staged AGI {version} did not pass its version check.")


def _sync_tree(root):
    for directory, _, files in os.walk(root, topdown=False):
        for name in files:
            with (Path(directory) / name).open("rb") as stream:
                os.fsync(stream.fileno())
        store._sync_directory(directory)


def _runtime_inventory(root):
    """Record owned paths without following links or crossing filesystem boundaries."""
    def walk_error(error):
        raise error

    paths = []
    device = root.stat().st_dev
    for folder, directories, files in os.walk(root, onerror=walk_error):
        for name in directories + files:
            path = Path(folder) / name
            details = path.lstat()
            if (details.st_dev != device or not (stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode))
                    or (stat.S_ISDIR(details.st_mode) and path.is_mount())):
                raise ValueError(f"Unexpected file in the managed AGI version: {path}")
            relative = path.relative_to(root)
            if relative == Path(RECEIPT):
                continue
            # Python creates this cache after installation, and only .pyc files belong inside it.
            if name == "__pycache__" and stat.S_ISDIR(details.st_mode):
                continue
            if relative.parent.name == "__pycache__" and name.endswith(".pyc") and stat.S_ISREG(details.st_mode):
                continue
            paths.append(relative.as_posix())
    return sorted(paths)


def _same_runtime(one, two):
    def files(root):
        return {path.relative_to(root) for path in root.rglob("*")
                if path.is_file() and path.name != RECEIPT and "__pycache__" not in path.parts}

    paths = files(one)
    if paths != files(two):
        return False
    for path in paths:
        source, target = one / path, two / path
        if (target.resolve() != target or source.read_bytes() != target.read_bytes()
                or os.access(source, os.X_OK) != os.access(target, os.X_OK)):
            return False
    return True


def _activate(source, prefix, bin_dir, version, expected=None):
    if (source / "VERSION").read_text(encoding="ascii").strip() != version:
        raise ValueError("The release VERSION does not match the selected version.")
    _check_placement(prefix, bin_dir)
    prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
    with store._file_lock(prefix / "install.lock"):
        active = _current(prefix)
        if expected is not None and active != expected:
            raise ValueError("AGI changed while the update was downloading. Run agi update again.")
        if active is not None:
            existing_bin = _read_receipt(active)
            if existing_bin != bin_dir:
                raise ValueError(f"Use the existing command directory, {existing_bin}.")
            if release_assets.version_key(active.name) > release_assets.version_key(version):
                raise ValueError(f"AGI {active.name} is already installed.")
        _check_placement(prefix, bin_dir)
        versions = prefix / "versions"
        versions.mkdir(exist_ok=True, mode=0o700)
        target = versions / version
        with tempfile.TemporaryDirectory(prefix=".install-", dir=versions) as scratch:
            scratch = Path(scratch)
            staged = scratch / "runtime"
            staged.mkdir()
            _copy_runtime(source, staged)
            _check_runtime(staged, version, scratch / "conversation")
            if target.exists() or target.is_symlink():
                if target.is_symlink() or _read_receipt(target) != bin_dir or not _same_runtime(staged, target):
                    raise ValueError(f"{target} already contains a different copy of AGI {version}.")
            else:
                store._atomic_json(staged / RECEIPT, {
                    "format": 1, "version": version, "bin_dir": str(bin_dir),
                    "paths": _runtime_inventory(staged),
                })
                _sync_tree(staged)
                staged.rename(target)
                store._sync_directory(versions)

            bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            for command in LAUNCHERS:
                link = bin_dir / command
                destination = prefix / "current" / "bin" / command
                try:
                    link.symlink_to(destination)
                except FileExistsError:
                    if not link.is_symlink() or link.readlink() != destination:
                        raise
            store._sync_directory(bin_dir)
            if active != target:
                with tempfile.TemporaryDirectory(prefix=".activate-", dir=prefix) as activation:
                    pointer = Path(activation) / "current"
                    pointer.symlink_to(Path("versions") / version)
                    pointer.replace(prefix / "current")
                store._sync_directory(prefix)
            if active is None:
                store._sync_directory(prefix.parent)
                store._sync_directory(bin_dir.parent)
    return bin_dir / "agi"


def install(prefix=None, bin_dir=None):
    prefix = (Path(prefix).expanduser() if prefix else _default_prefix()).resolve()
    bin_dir = (Path(bin_dir).expanduser() if bin_dir else Path.home() / ".local/bin").resolve()
    path = _activate(SOURCE, prefix, bin_dir, __version__)
    print(f"Installed AGI {__version__}.")
    if str(bin_dir) not in os.get_exec_path():
        print(f"Run {shlex.quote(str(path))}, or add {shlex.quote(str(bin_dir))} to PATH.")
    else:
        print("Run agi to open the chat.")


def _managed_install(command, checkout_hint=""):
    prefix = SOURCE.parent.parent
    if SOURCE.parent.name != "versions" or not (SOURCE / RECEIPT).is_file():
        raise ValueError(f"Run agi {command} from a managed install.{checkout_hint}")
    bin_dir = _read_receipt(SOURCE)
    if _current(prefix) != SOURCE:
        raise ValueError(f"Run the current agi command to {command} this installation.")
    return prefix, bin_dir


def check_update(requested=None):
    """Find the update to present for confirmation without changing the installation."""
    _managed_install("update", " From a checkout, update the checkout.")
    return release_assets.find(__version__, requested)


def update_notes(release):
    return "".join(char for char in release.notes[:4000] if char.isprintable() or char in "\n\t")


def apply_update(release):
    """Install the exact offer the user reviewed and return the stable launcher."""
    prefix, bin_dir = _managed_install("update")
    if release_assets.version_key(release.version) <= release_assets.version_key(__version__):
        raise ValueError("The selected release is already installed or older.")
    with tempfile.TemporaryDirectory(prefix=".download-", dir=prefix) as scratch:
        scratch = Path(scratch)
        archive = scratch / release.name
        release_assets.download(release, archive)
        source = release_assets.unpack(release, archive, scratch)
        return _activate(source, prefix, bin_dir, release.version, expected=SOURCE)


def update(*, check=False, yes=False, requested=None):
    release = check_update(requested)
    if release is None:
        print(f"AGI {__version__} is up to date on this release channel.")
        return
    print(f"AGI {__version__} → {release.version}")
    if notes := update_notes(release):
        print(notes)
    if check:
        return
    if not yes:
        if not sys.stdin.isatty():
            raise ValueError("Run agi update in a terminal or pass --yes to apply the update.")
        with suppress(EOFError):
            yes = input("Install this update? [y/N] ").strip().lower() in ("y", "yes")
        if not yes:
            return
    apply_update(release)
    print(f"Installed AGI {release.version}. Reopen the chat screen when convenient to use it.")


def _uninstall_paths(prefix, bin_dir):
    if bin_dir.resolve() != bin_dir:
        raise ValueError(f"The AGI command directory now points somewhere else: {bin_dir}")
    _check_placement(prefix, bin_dir)
    versions = prefix / "versions"
    if versions.is_mount():
        raise ValueError(f"AGI cannot remove a mounted versions folder: {versions}")
    for version in sorted(versions.iterdir()):
        if (version.is_symlink() or version.is_mount() or not version.is_dir()
                or not release_assets.VERSION.fullmatch(version.name)):
            raise ValueError(f"AGI cannot remove this unrecognized version folder: {version}")
        receipt = _receipt(version)
        if Path(receipt["bin_dir"]) != bin_dir or receipt.get("paths") != _runtime_inventory(version):
            raise ValueError(f"AGI cannot verify the installed files in {version}.")
    return versions


def _require_stopped(prefix, bin_dir):
    """Check for managed launchers, watchers and provider children on Linux."""
    root = os.fsencode(prefix)
    launchers = {os.fsencode(bin_dir / name) for name in LAUNCHERS}
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        paths = []
        with suppress(FileNotFoundError, ProcessLookupError, PermissionError):
            paths.extend((process / "cmdline").read_bytes().split(b"\0"))
        with suppress(FileNotFoundError, ProcessLookupError, PermissionError):
            paths.append(os.fsencode(os.readlink(process / "cwd")))
        with suppress(FileNotFoundError, ProcessLookupError, PermissionError):
            for setting in (process / "environ").read_bytes().split(b"\0"):
                if setting.startswith(b"PYTHONPATH="):
                    paths.extend(setting.removeprefix(b"PYTHONPATH=").split(b":"))
        if any(path in launchers or path == root or path.startswith(root + b"/") for path in paths):
            raise ValueError("Close AGI and its connected agent sessions before uninstalling. "
                             "Move any terminals out of the install folder.")


def _disconnect(command, **kwargs):
    try:
        result = subprocess.run(command, timeout=15, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"{Path(command[0]).name} did not respond. Try uninstalling again.") from error
    if command[1:3] == ["mcp", "remove"] and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"{Path(command[0]).name} could not remove its AGI registration"
                         + (f": {detail}" if detail else "."))
    return result


def uninstall(*, check=False, yes=False):
    """Preview or remove this managed installation while preserving conversation data."""
    from agi import connect

    prefix, bin_dir = _managed_install("uninstall")
    _uninstall_paths(prefix, bin_dir)
    print(f"Remove the managed AGI versions in {prefix / 'versions'}.")
    print(f"Remove {bin_dir / 'agi'} and {bin_dir / 'agi-mcp'}.")
    print("Remove only Claude Code and Codex registrations for this installation.")
    print("Conversation data and backups stay in place.")
    if check:
        return
    if not yes:
        if not sys.stdin.isatty():
            raise ValueError("Run agi uninstall in a terminal or pass --yes to remove this installation.")
        with suppress(EOFError):
            yes = input("Uninstall AGI? [y/N] ").strip().lower() in ("y", "yes")
        if not yes:
            return
    lock = prefix / "install.lock"
    if lock.is_symlink():
        raise ValueError(f"AGI cannot use a symbolic link for its install lock: {lock}")
    with store._file_lock(lock):
        if _managed_install("uninstall") != (prefix, bin_dir):
            raise ValueError("The AGI installation changed before removal. Run agi uninstall again.")
        versions = _uninstall_paths(prefix, bin_dir)
        _require_stopped(prefix, bin_dir)
        # Withdraw the stable path before reclaiming its code. Interrupted cleanup cannot leave the
        # launcher pointing into a partially removed version, and install.lock keeps other installers serialized.
        with tempfile.TemporaryDirectory(prefix=".uninstall-", dir=prefix) as retired:
            retired = Path(retired)
            pointer = prefix / "current"
            held_pointer = retired / "current"
            retired_versions = retired / "versions"
            pointer.rename(held_pointer)
            try:
                _require_stopped(prefix, bin_dir)
                for client in connect.CLIENTS:
                    print(connect.disconnect(client, root=pointer, run=_disconnect))
                _require_stopped(prefix, bin_dir)
                _uninstall_paths(prefix, bin_dir)
                versions.rename(retired_versions)
            except BaseException:
                if retired_versions.is_dir():
                    retired_versions.rename(versions)
                held_pointer.rename(pointer)
                store._sync_directory(prefix)
                raise
            store._sync_directory(prefix)
            for name in LAUNCHERS:
                launcher = bin_dir / name
                if launcher.is_symlink() and launcher.readlink() == pointer / "bin" / name:
                    launcher.unlink()
            if bin_dir.is_dir():
                store._sync_directory(bin_dir)
            store._sync_directory(prefix)
    print("Uninstalled AGI.")
