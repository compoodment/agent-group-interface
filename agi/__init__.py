"""AGI: a tiny local group chat for the user, Claude and Codex."""
from pathlib import Path

# VERSION is the single source for CLI, help and release tags.
__version__ = (Path(__file__).resolve().parents[1] / 'VERSION').read_text(encoding='ascii').strip()
