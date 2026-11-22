"""Desktop popups shared by every open chat screen: one popup per mention, none while any screen is focused.

Screens of the same chat coordinate through Linux abstract Unix sockets: names that live in the kernel,
never files, and vanish when the screen's process ends, even if it is killed. The screen holding the
notifier name sends the popups. A screen whose terminal reported focus holds a focus name; while any focus
name exists, nobody pops up, because the user is looking at the chat.
"""

import hashlib
import os
import shutil
import socket
import subprocess
import threading

from agi import store
from agi.ui import sound


class Desktop:
    def __init__(self):
        chat = hashlib.sha256(str(store.DATA_DIR.absolute()).encode()).hexdigest()[:12]
        self.prefix = f"agi-{os.getuid()}-{chat}-"     # screens of another chat (tests) never coordinate
        self.notifier = None             # socket: this screen sends the popups
        self.focus = None                # socket: this screen's terminal reported focus

    def focused(self, focused):
        """From the terminal's focus reports only: an assumed focus must not silence the other screens."""
        if focused and self.focus is None:
            self.focus = _claim(f"{self.prefix}focused-{os.getpid()}")
        elif not focused and self.focus is not None:
            self.focus.close()
            self.focus = None

    def notify(self, title, body, message=None):
        """Pop up with the mention sound (unless /sound off); not when popups are off, any screen of this chat is
        focused, or another screen sends them."""
        command = os.environ.get("AGI_NOTIFY_CMD", "notify-send")
        if os.environ.get("AGI_NOTIFY") == "0" or not shutil.which(command):
            return False
        if self.focus is not None or self._someone_focused():
            return False
        if self.notifier is None:
            self.notifier = _claim(f"{self.prefix}notifier")
            if self.notifier is None:    # another screen holds the name: it pops up for this mention
                return False
        threading.Thread(target=subprocess.run, args=([command, "-a", "AGI", "--", title, body],), daemon=True,
                         kwargs={"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}).start()
        if sound.mode() != "off":        # with the popup, from the one screen that sends it: one sound per mention
            sound.play()
            if message is not None:
                sound.log("sound and popup", message, "for you while away")
        return True

    def _someone_focused(self):
        marker = f"@{self.prefix}focused-"
        try:
            with open("/proc/net/unix") as table:    # abstract names appear here with a leading @
                return any(line.rstrip("\n").rsplit(" ", 1)[-1].startswith(marker) for line in table)
        except OSError:
            return False


def _claim(name):
    """Bind an abstract socket name; None when another process already holds it."""
    held = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        held.bind("\0" + name)
    except OSError:
        held.close()
        return None
    return held
