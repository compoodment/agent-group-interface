"""Images for the chat screen, through GTK in a short-lived helper process so the screen itself never loads GTK.

    file_png(path)           an image file (any format GdkPixbuf reads) as PNG bytes, or None

The clipboard, images included, is clipboard.py's, through a helper of the same kind.

The helper is this module run with python3 -m; each call times out, and a timeout says so (ValueError);
any other failure is None, never an error on screen: a paste that isn't an image just stays text.
The one exception: no GTK for Python at all (python3-gi, or its typelibs, missing) makes the helper exit with
NO_GTK, and the caller gets ImageSupportMissing (a ValueError) so the screen can say so once.
"""
import os
import subprocess
import sys

TIMEOUT_S = 15               # Loading GTK and decoding a valid image can take several seconds under load.
MAX_PIXELS = 16_000_000      # the store's own limit: a bigger file isn't decoded at all
NO_GTK = 3                   # the helper's exit code for "gi or its typelibs aren't installed", not "not an image"
# two notes (Codex, #6145): Ctrl+V can't read any clipboard without GTK, text included; a dropped picture can't be read
CLIPBOARD_NOTE = ("Couldn't read the clipboard: Ctrl+V needs GTK 3 for Python (python3-gi). Your terminal's own "
                  "paste (often Ctrl+Shift+V) works for text.")
MISSING_NOTE = "Attaching pictures needs GTK 3 for Python (python3-gi)."
ADDRESS_SPACE = 1536 << 20   # the helper's memory cap, set before GTK loads or anything is decoded (Codex, #2147)


class ImageSupportMissing(ValueError):
    """This machine can't handle images at all (no GTK 3 for Python), as opposed to "that wasn't an image"."""

    def __init__(self):
        super().__init__(MISSING_NOTE)


def load_gtk(*namespaces):
    """gi.repository modules for the helpers; ImageSupportMissing when gi or a typelib is not installed."""
    try:
        import gi
        for name, version in namespaces:
            gi.require_version(name, version)
        import importlib
        return [importlib.import_module("gi.repository." + name) for name, _ in namespaces]
    except (ImportError, ValueError):       # require_version raises ValueError for a missing typelib
        raise ImageSupportMissing from None


def _helper(*args, timeout=TIMEOUT_S):
    try:
        done = subprocess.run([sys.executable, "-m", "agi.ui.pixbuf", *map(str, args)], capture_output=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    except subprocess.TimeoutExpired:
        raise ValueError("reading the image took too long; try again") from None
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == NO_GTK:
        raise ImageSupportMissing
    return done.stdout if done.returncode == 0 and done.stdout else None


def file_png(path):
    return _helper("file", path)


def _png(pixbuf):
    ok, buffer = pixbuf.save_to_bufferv("png", [], [])
    return bytes(buffer) if ok else b""


def _main(command, *args):
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE, ADDRESS_SPACE))
    _, GdkPixbuf = load_gtk(("Gdk", "3.0"), ("GdkPixbuf", "2.0"))
    info, width, height = GdkPixbuf.Pixbuf.get_file_info(args[0])     # the header only: no decode yet
    if info is None or width * height > MAX_PIXELS:
        return b""
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(args[0])
    pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
    if command == "file":
        return _png(pixbuf)
    return b""


if __name__ == "__main__":
    try:
        sys.stdout.buffer.write(_main(*sys.argv[1:]))
    except ImageSupportMissing:
        sys.exit(NO_GTK)
    except Exception:          # noqa: BLE001 - no image is the answer, whatever went wrong
        sys.exit(1)
