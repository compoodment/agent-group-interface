"""The process that holds copied text on the X clipboard, speaking to X directly through libX11 and ctypes so it
does not load GTK in the process that holds the clipboard.

clipboard.copy() runs this file by its path, `python3 -I -S clipboard_owner.py <address space>`, with the text on
stdin: by path, not with -m, so the agi.ui package (the whole screen, ~15 MB) never loads here. It forks the owner
first, in its own session with nothing inherited open; the owner caps its memory, connects to $DISPLAY, takes
CLIPBOARD with a real server timestamp (ICCCM forbids CurrentTime), says READY on a pipe, and serves the text
until another program takes the clipboard or the display goes away, then exits. The first process passes the
owner's word on to stdout and ends, so copy() never waits for the owner.

It offers UTF8_STRING, text/plain;charset=utf-8, STRING (Latin-1, `?` for the rest) and TEXT (STRING when the
text is all Latin-1, else UTF8_STRING), and lists them under TARGETS; any other target is refused. Text bigger
than one X request carries is refused at copy time rather than sent in pieces (no INCR).
"""
import ctypes
import os
import sys

READY = b"ok"
CHECK_EVERY_S = 10               # also look this often, in case the server never said the clipboard was taken
TOO_MUCH = "too much text to copy at once"
MARGIN = 4096                    # bytes of the largest request that aren't the text: its header, with room to spare

# X.h and Xatom.h
NONE = CURRENT_TIME = 0
PROPERTY_CHANGE_MASK = 1 << 22
PROPERTY_NOTIFY, SELECTION_CLEAR, SELECTION_REQUEST, SELECTION_NOTIFY = 28, 29, 30, 31
PROP_MODE_REPLACE, PROP_MODE_APPEND = 0, 2
XA_ATOM, XA_STRING = 4, 31

# Xlib.h's event structs. Window, Atom and Time are unsigned long on the client side, Bool an int, so on 64 bits
# every event starts with type at 0, serial at 8, send_event at 16 and display at 24 (checked against libx11-dev
# 2:1.8.12's Xlib.h; tests/test_ui.py holds the offsets).
_HEAD = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
         ("display", ctypes.c_void_p)]


class PropertyEvent(ctypes.Structure):
    _fields_ = _HEAD + [("window", ctypes.c_ulong), ("atom", ctypes.c_ulong), ("time", ctypes.c_ulong),
                        ("state", ctypes.c_int)]


class SelectionClearEvent(ctypes.Structure):
    _fields_ = _HEAD + [("window", ctypes.c_ulong), ("selection", ctypes.c_ulong), ("time", ctypes.c_ulong)]


class SelectionRequestEvent(ctypes.Structure):
    _fields_ = _HEAD + [("owner", ctypes.c_ulong), ("requestor", ctypes.c_ulong), ("selection", ctypes.c_ulong),
                        ("target", ctypes.c_ulong), ("property", ctypes.c_ulong), ("time", ctypes.c_ulong)]


class SelectionEvent(ctypes.Structure):
    _fields_ = _HEAD + [("requestor", ctypes.c_ulong), ("selection", ctypes.c_ulong), ("target", ctypes.c_ulong),
                        ("property", ctypes.c_ulong), ("time", ctypes.c_ulong)]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xproperty", PropertyEvent), ("xselectionclear", SelectionClearEvent),
                ("xselectionrequest", SelectionRequestEvent), ("xselection", SelectionEvent),
                ("pad", ctypes.c_long * 24)]


_IO_ERROR = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
_ERROR = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def _xlib():
    """libX11 with the signatures this file calls: pointers and XIDs are 64 bits, never ctypes' default int."""
    x = ctypes.CDLL("libX11.so.6")
    display, xid, event = ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XEvent)
    for name, result, arguments in (
            ("XOpenDisplay", display, [ctypes.c_char_p]),
            ("XSetIOErrorHandler", ctypes.c_void_p, [_IO_ERROR]),
            ("XSetErrorHandler", ctypes.c_void_p, [_ERROR]),
            ("XMaxRequestSize", ctypes.c_long, [display]),
            ("XExtendedMaxRequestSize", ctypes.c_long, [display]),
            ("XDefaultRootWindow", xid, [display]),
            ("XCreateSimpleWindow", xid, [display, xid, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                          ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]),
            ("XInternAtom", xid, [display, ctypes.c_char_p, ctypes.c_int]),
            ("XSelectInput", ctypes.c_int, [display, xid, ctypes.c_long]),
            ("XChangeProperty", ctypes.c_int, [display, xid, xid, xid, ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                               ctypes.c_int]),
            ("XWindowEvent", ctypes.c_int, [display, xid, ctypes.c_long, event]),
            ("XSetSelectionOwner", ctypes.c_int, [display, xid, xid, xid]),
            ("XGetSelectionOwner", xid, [display, xid]),
            ("XConnectionNumber", ctypes.c_int, [display]),
            ("XPending", ctypes.c_int, [display]),
            ("XNextEvent", ctypes.c_int, [display, event]),
            ("XSendEvent", ctypes.c_int, [display, xid, ctypes.c_int, ctypes.c_long, event]),
            ("XFlush", ctypes.c_int, [display])):
        function = getattr(x, name)
        function.restype, function.argtypes = result, arguments
    return x


def _own(data, report, address_space):
    """The owner: take the clipboard, say so on `report`, then serve `data` (UTF-8) until another program takes it
    or the display closes."""
    import resource
    import select
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    if not os.environ.get("DISPLAY"):
        raise ValueError("no desktop display to copy to (DISPLAY isn't set)")
    x = _xlib()
    # Xlib's own handlers end the process with exit() and a message nobody sees: a lost display ends the owner at
    # once, never lingering past logout, and a requestor that vanished mid-answer costs only that answer
    handlers = _IO_ERROR(lambda _display: os._exit(0)), _ERROR(lambda _display, _error: 0)
    x.XSetIOErrorHandler(handlers[0])
    x.XSetErrorHandler(handlers[1])
    display = x.XOpenDisplay(None)
    if not display:
        raise ValueError("no desktop display to copy to")
    try:                                 # only the UTF-8 is kept; Latin-1 is made when something asks for it
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", "replace")
        data = text.encode("utf-8")
    if len(data) > (x.XExtendedMaxRequestSize(display) or x.XMaxRequestSize(display)) * 4 - MARGIN:
        raise ValueError(TOO_MUCH)
    plain = not text or max(text) < "\u0100"                        # the text is all Latin-1
    del text

    def latin():
        return data.decode("utf-8").encode("latin-1", "replace")

    def atom(name):
        return x.XInternAtom(display, name, False)

    clipboard, targets, text_atom, utf8 = atom(b"CLIPBOARD"), atom(b"TARGETS"), atom(b"TEXT"), atom(b"UTF8_STRING")
    mime = atom(b"text/plain;charset=utf-8")
    offers = {utf8: (utf8, data), mime: (mime, data), XA_STRING: (XA_STRING, latin),
              text_atom: (XA_STRING, latin) if plain else (utf8, data)}
    listed = (ctypes.c_ulong * (len(offers) + 1))(targets, *offers)
    offers[targets] = XA_ATOM, bytes(listed)

    window = x.XCreateSimpleWindow(display, x.XDefaultRootWindow(display), 0, 0, 1, 1, 0, 0, 0)   # never mapped
    event = XEvent()
    x.XSelectInput(display, window, PROPERTY_CHANGE_MASK)       # the server's time now: a property change says it
    x.XChangeProperty(display, window, atom(b"AGI_CLIPBOARD_TIME"), XA_STRING, 8, PROP_MODE_APPEND, b"", 0)
    x.XWindowEvent(display, window, PROPERTY_CHANGE_MASK, ctypes.byref(event))
    acquired = event.xproperty.time
    x.XSetSelectionOwner(display, clipboard, window, acquired)
    if x.XGetSelectionOwner(display, clipboard) != window:
        raise ValueError("another program kept the clipboard")
    os.write(report, READY + f" {os.getpid()}".encode())
    os.close(report)

    connection = x.XConnectionNumber(display)
    while True:
        if not x.XPending(display):                            # nothing queued: wait on the socket itself
            if not select.select([connection], [], [], CHECK_EVERY_S)[0] and \
                    x.XGetSelectionOwner(display, clipboard) != window:
                return
            continue
        x.XNextEvent(display, ctypes.byref(event))
        if event.type == SELECTION_CLEAR:
            return
        if event.type == SELECTION_REQUEST:
            _answer(x, display, event.xselectionrequest, offers, acquired)


def _answer(x, display, request, offers, acquired):
    """Put the asked-for form of the text on the requestor's window and tell it, or tell it no (property None)."""
    offer = offers.get(request.target)
    early = request.time != CURRENT_TIME and (request.time - acquired) & 0xFFFFFFFF >= 1 << 31   # before we owned it
    property = request.property or request.target               # an obsolete requestor names none: ICCCM 2.2
    if offer is None or early:
        property = NONE
    else:
        kind, value = offer
        value = value() if callable(value) else value
        form, size = (32, ctypes.sizeof(ctypes.c_ulong)) if kind == XA_ATOM else (8, 1)   # Xlib's 32 is a long
        x.XChangeProperty(display, request.requestor, property, kind, form, PROP_MODE_REPLACE, value,
                          len(value) // size)
    reply = XEvent()
    said = reply.xselection
    said.type, said.send_event, said.display, said.requestor = SELECTION_NOTIFY, True, display, request.requestor
    said.selection, said.target, said.property, said.time = request.selection, request.target, property, request.time
    x.XSendEvent(display, request.requestor, False, 0, ctypes.byref(reply))
    x.XFlush(display)


def _copy(data, address_space):
    """Fork the owner before anything loads libX11, wait for its word, and pass it on."""
    readable, writable = os.pipe()
    pid = os.fork()
    if pid == 0:                             # the owner: its own session, no terminal, nothing inherited open
        os.close(readable)
        os.setsid()
        os.chdir("/")                        # never keeps a drive or a test's folder busy
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        try:
            _own(data, writable, address_space)
            status = 0
        except Exception as error:      # noqa: BLE001 - said on the pipe, then the owner ends
            try:
                os.write(writable, str(error).encode() or b"the clipboard helper failed")
            except OSError:
                pass
            status = 1
        os._exit(status)
    os.close(writable)
    said = b""
    while chunk := os.read(readable, 4096):
        said += chunk
    os.close(readable)
    if not said.startswith(READY):           # the owner failed and has ended, or will: collect it
        os.waitpid(pid, 0)
    return said


if __name__ == "__main__":
    said = _copy(sys.stdin.buffer.read(), int(sys.argv[1]))
    sys.stdout.buffer.write(said)
    sys.exit(0 if said.startswith(READY) else 1)
