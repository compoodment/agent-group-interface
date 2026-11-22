"""The desktop clipboard, in helper processes so the screen itself never loads GTK, as pixbuf.py does for images.

    copy(text)    puts text on the X clipboard; raises ValueError saying why it couldn't
    read()        ("image", PNG bytes), ("text", str), or None for an empty or unreadable clipboard;
                  ImageSupportMissing (a ValueError) when GTK for Python isn't installed

The clipboard owner holds text through libX11 until another program takes it.
Only the latest owner remains running. read() is short-lived, so it uses GTK, which also turns a copied image into PNG.
"""
import os
import subprocess
import sys

from agi.ui import clipboard_owner
from agi.ui.pixbuf import ADDRESS_SPACE, NO_GTK, TIMEOUT_S, ImageSupportMissing, load_gtk


def _helper(arguments, data=b"", timeout=TIMEOUT_S):
    try:
        return subprocess.run([sys.executable, *arguments], input=data, capture_output=True, timeout=timeout,
                              cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    except subprocess.TimeoutExpired:
        raise ValueError("the clipboard took too long; try again") from None
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"the clipboard helper didn't start ({error})") from None


def copy(text):
    """Offer `text` on the clipboard until something else is copied. Returns the owner's process id."""
    # by path, isolated and without site: the owner loads nothing it doesn't use, not even this package
    done = _helper(["-I", "-S", clipboard_owner.__file__, str(ADDRESS_SPACE)], text.encode("utf-8"))
    said = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not said.startswith(clipboard_owner.READY.decode()):
        raise ValueError(said or "the clipboard helper failed")
    return int(said.split()[1])


def read():
    try:
        done = _helper(["-m", "agi.ui.clipboard", "read"])
    except ValueError as error:
        if "too long" in str(error):
            raise
        return None
    if done.returncode == NO_GTK:       # no GTK for Python: the caller says so (once), not "empty"
        raise ImageSupportMissing
    if done.returncode != 0 or not done.stdout:
        return None
    kind, data = done.stdout[:1], done.stdout[1:]
    if kind == b"I":
        return "image", data
    return "text", data.decode("utf-8", "replace")


def _gtk():
    """GTK for read(): under X, where the owner is, also on a Wayland desktop, where a window without focus may
    not use the clipboard; never the accessibility bus, which can hang a windowless process."""
    os.environ["GDK_BACKEND"] = "x11"
    os.environ["NO_AT_BRIDGE"] = "1"
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE, ADDRESS_SPACE))
    Gdk, Gtk = load_gtk(("Gdk", "3.0"), ("Gtk", "3.0"))
    if not Gtk.init_check([])[0] or Gdk.Display.get_default() is None:
        raise ValueError("no desktop display to read from")
    return Gdk, Gtk


def _read():
    Gdk, Gtk = _gtk()
    board = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    if board.wait_is_image_available():
        from agi.ui.pixbuf import _png
        image = board.wait_for_image()
        if image is not None:
            return b"I" + _png(image)
    text = board.wait_for_text()
    return b"T" + text.encode("utf-8") if text else b""


if __name__ == "__main__":
    try:
        sys.stdout.buffer.write(_read() if sys.argv[1:] == ["read"] else b"")
    except ImageSupportMissing:
        sys.exit(NO_GTK)
    except Exception:          # noqa: BLE001 - nothing readable is the answer, whatever went wrong
        sys.exit(1)
