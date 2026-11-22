"""Modified-key reports, decoded into the keys the chat already understands.

Terminals send Shift+Enter exactly like Enter unless asked otherwise. At startup the chat asks for kitty's
"disambiguate" mode and xterm's modifyOtherKeys level 1 (terminals without them ignore both). Terminals with
them then report modified keys as CSI sequences: `CSI 13;2u` (kitty) or `CSI 27;2;13~` (xterm) for Shift+Enter.
In kitty's mode Esc and Ctrl/Alt+keys arrive as CSI u too, so those are decoded back here as well. So do the
arrows, Home, End, Delete and Page Up/Down: that mode sends them as CSI A or CSI 3~, not the SS3 form curses
decodes itself, and a terminal in normal cursor mode does the same.
"""

import curses
import re

ENABLE = "\x1b[>1u\x1b[>4;1m"          # push kitty's disambiguate flag; xterm modifyOtherKeys level 1
DISABLE = "\x1b[<u\x1b[>4m"            # pop it; modifyOtherKeys back to the terminal's default
KITTY = re.compile(r"\[(\d+)(?::[\d:]*)?(?:;(\d+)(?::\d+)?)?(?:;[\d:]*)?u")
XTERM = re.compile(r"\[27;(\d+);(\d+)~")
CURSOR = re.compile(r"[\[O](?:1;(\d+)(?::\d+)?)?([ABCDHF])")   # CSI A, CSI 1;5D (Ctrl+Left), SS3 A
NUMBERED = re.compile(r"\[(\d+)(?:;(\d+)(?::\d+)?)?~")          # CSI 3~ (Delete), CSI 3;5~
SHIFT, ALT, CTRL = 1, 2, 4
ENTERS = (13, 57414)                    # Enter, keypad Enter
LETTERS = {"A": curses.KEY_UP, "B": curses.KEY_DOWN, "C": curses.KEY_RIGHT, "D": curses.KEY_LEFT,
           "H": curses.KEY_HOME, "F": curses.KEY_END}
NUMBERS = {1: curses.KEY_HOME, 7: curses.KEY_HOME, 4: curses.KEY_END, 8: curses.KEY_END, 3: curses.KEY_DC,
           5: curses.KEY_PPAGE, 6: curses.KEY_NPAGE}
HISTORY = {"A": curses.KEY_SR, "B": curses.KEY_SF}      # curses' own Shift+Up/Down, which Composer.key handles
WORDS = {"D": "b", "C": "f"}            # Ctrl/Alt+Left/Right move by word, as Alt+B/F do
PRIVATE = range(57344, 63744)           # kitty's codes for keys that type nothing (keypad, media, modifiers)


def decode(sequence):
    """For the characters after ESC up to the final byte: ("newline",), ("escape",), ("alt", char),
    ("key", key for Composer.key) or None for a key the chat has no use for."""
    cursor, numbered = CURSOR.fullmatch(sequence), NUMBERED.fullmatch(sequence)
    if cursor:
        modifiers, letter = int(cursor.group(1) or 1) - 1, cursor.group(2)
        if modifiers & (ALT | CTRL) and letter in WORDS:
            return ("alt", WORDS[letter])
        if modifiers & (SHIFT | ALT | CTRL) and letter in HISTORY:
            return ("key", HISTORY[letter])     # Shift/Alt/Ctrl+Up/Down walk the sent history
        return ("key", LETTERS[letter])
    if numbered:
        key = NUMBERS.get(int(numbered.group(1)))
        return None if key is None else ("key", key)
    kitty, xterm = KITTY.fullmatch(sequence), XTERM.fullmatch(sequence)
    if kitty:
        code, modifiers = int(kitty.group(1)), int(kitty.group(2) or 1) - 1
    elif xterm:
        modifiers, code = int(xterm.group(1)) - 1, int(xterm.group(2))
    else:
        return None
    if code in ENTERS:
        return ("newline",) if modifiers & (SHIFT | ALT | CTRL) else ("key", "\r")
    if code in PRIVATE:
        return None
    if code == 27:
        return ("escape",)
    if code == 9:
        return ("key", "\t")
    if code in (8, 127):
        return ("alt", "\x7f") if modifiers & (ALT | CTRL) else ("key", "\x7f")
    if modifiers & CTRL and 64 <= (code & ~32) <= 95:
        return ("key", chr(code & 31))  # Ctrl+C is ^C again, Ctrl+J a new line, as without the mode
    if modifiers & ALT and 32 < code < 127:
        return ("alt", chr(code))
    if 32 <= code < 0x110000 and not modifiers & CTRL:
        return ("key", chr(code))
    return None
