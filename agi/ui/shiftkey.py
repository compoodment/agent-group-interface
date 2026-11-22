"""Whether Shift is held right now, for Shift+Enter (a new line instead of sending).

This terminal (VTE) sends Enter and Shift+Enter as the same byte, so the chat asks the X server for the keyboard's
modifier state the moment Enter arrives (libX11 through ctypes: no new software). Without X, as on a console or
over SSH, it answers False and Enter sends as before. Xlib ends the whole process when a connection it holds
dies (an SSH or tmux session outliving the desktop left the terminal raw; UI bug hunt, 2026-11-18), so the
connection's socket is checked first: one the server has already closed is dropped without another Xlib call
and opened again later. A server dying between that check and the query can still end the process; only
a query that fails safely would cover that window (Codex, #1447). AGI_SHIFT_ENTER=0 turns it off. For tests,
AGI_SHIFT_FILE names a file whose existence stands in for a held Shift.
"""

import ctypes
import ctypes.util
import os
import socket

SHIFT_MASK = 1
_x = None                               # (libX11, display, root) once opened; False when there's no X to ask


def shift_held():
    stand_in = os.environ.get("AGI_SHIFT_FILE")
    if stand_in:
        return os.path.exists(stand_in)
    global _x
    if _x is None:
        _x = _open()
    if _x and not _alive(_x[1], _x[0]):
        _x = _open()                    # the old connection is left alone: closing it would end the process
    if not _x:
        return False
    library, display, root = _x
    root_found, child = ctypes.c_ulong(), ctypes.c_ulong()
    root_x, root_y, x, y = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    mask = ctypes.c_uint()
    if not library.XQueryPointer(display, root, ctypes.byref(root_found), ctypes.byref(child), ctypes.byref(root_x),
                                 ctypes.byref(root_y), ctypes.byref(x), ctypes.byref(y), ctypes.byref(mask)):
        return False
    return bool(mask.value & SHIFT_MASK)


def _alive(display, library):
    """Whether the X server is still at the other end: a closed peer reads as end-of-file."""
    try:
        with socket.socket(fileno=os.dup(library.XConnectionNumber(display))) as peer:
            return peer.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) != b""
    except BlockingIOError:             # nothing waiting to be read: open and quiet
        return True
    except OSError:
        return False


def _open():
    if os.environ.get("AGI_SHIFT_ENTER") == "0" or not os.environ.get("DISPLAY"):
        return False
    name = ctypes.util.find_library("X11")
    if not name:
        return False
    try:
        library = ctypes.cdll.LoadLibrary(name)
    except OSError:
        return False
    library.XOpenDisplay.restype = ctypes.c_void_p
    library.XOpenDisplay.argtypes = [ctypes.c_char_p]
    library.XDefaultRootWindow.restype = ctypes.c_ulong
    library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    library.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
    library.XConnectionNumber.argtypes = [ctypes.c_void_p]
    display = library.XOpenDisplay(None)
    if not display:
        return False
    return library, display, library.XDefaultRootWindow(display)
