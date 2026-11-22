"""Curses chat screen for the user. Owner: Claude.

Layout follows the UI audit's Option A ("Claude Code transcript"). The screen only displays and sends: it never
moves read positions or applies agent rules. Modules: text (measuring, wrapping), render (how it looks),
composer (the input box), screen (state), app (main loop).
"""

from agi.ui.app import main
from agi.ui.screen import Screen
from agi.ui.text import cell_width, clean, fit, parse_duration, text_width, wrap

__all__ = ["Screen", "cell_width", "clean", "fit", "main", "parse_duration", "text_width", "wrap"]
