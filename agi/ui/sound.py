"""The mention sound: whether it is on (remembered across starts, shared by every screen of this chat) and playing it.

By default it plays with the desktop popup, so only while no screen of this chat is focused; "always" also plays it in the focused screen, and "off" silences it (#5440). The
sound is the desktop's own "new message" one, played by PulseAudio's player; without either it stays silent.
"""

import json
import os
import shutil
import subprocess
import time

from agi import store, ui_state

VERSION = 1
SOUND = "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"


def _path():
    return ui_state._directory() / "sound.json"


MODES = ("away", "always", "off")


def mode():
    """"away" (the default: only while no screen of this chat is focused), "always" or "off". An unreadable setting
    counts as the default, so a mention is never silently lost."""
    try:
        value = json.loads(_path().read_text())
        if isinstance(value, dict) and value.get("version") == VERSION and value.get("mode") in MODES:
            return value["mode"]
    except (OSError, ValueError):
        pass
    return "away"


def set_mode(value):
    if value not in MODES:
        raise ValueError(f"sound mode must be one of {', '.join(MODES)}")
    store._atomic_json(_path(), {"version": VERSION, "mode": value})


def play():
    """Start the sound and return at once; False when there is nothing to play it with."""
    command = os.environ.get("AGI_SOUND_CMD", "paplay")
    sound = os.environ.get("AGI_SOUND_FILE", SOUND)
    if not shutil.which(command) or not os.path.exists(sound):
        return False
    subprocess.Popen([command, sound], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


LOG_LIMIT = 64 * 1024                       # bytes kept: the newest lines, so the log never grows


def log(what, message, why):
    """Record the time, message and reason for each sound or bell in the local chat data folder."""
    try:
        path = ui_state._directory() / "sounds.log"
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what} #{message['id']} from {message['sender']}"
                f" ({message['kind']}): {why}\n")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line)
        if path.stat().st_size > LOG_LIMIT:
            kept = path.read_text(encoding="utf-8")[-LOG_LIMIT // 2:]
            path.write_text(kept[kept.find("\n") + 1:], encoding="utf-8")
    except (OSError, KeyError):             # a log must never cost a sound or a keystroke
        pass
