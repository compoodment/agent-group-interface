"""Read the documentation that ships with this running AGI package."""

from pathlib import Path
import re

from agi import __version__, updates
from agi.mcp_stdio import tool


TOPICS = Path(__file__).resolve().parents[1] / "docs" / "topics"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_TOPIC_BYTES = 64 * 1024
FRONT_MATTER = re.compile(
    r"\A---\nsummary: (?P<summary>[^\n]+)\nread_when: (?P<read_when>[^\n]+)\n---\n\n"
    r"(?P<markdown># (?P<title>[^\n]+)\n.+)\Z", re.DOTALL,
)

TOOL = tool("chat_help", "Read the running AGI version's documentation. Omit topic to list topics.",
            {"topic": dict(type="string")})
INSTRUCTIONS = ("Before answering how AGI works, use chat_help to check the running version's docs. "
                "Use project notes for that project's workflow. If you cannot check the AGI docs, "
                "say which details you could not verify.")


def _read_topic(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Documentation topic {path.stem!r} must be a bundled Markdown file")
    with path.open("rb") as source:
        contents = source.read(MAX_TOPIC_BYTES + 1)
    if len(contents) > MAX_TOPIC_BYTES:
        raise ValueError(f"Documentation topic {path.stem!r} is too large")
    try:
        match = FRONT_MATTER.fullmatch(contents.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError(f"Documentation topic {path.stem!r} must be UTF-8") from error
    if not match:
        raise ValueError(f"Documentation topic {path.stem!r} needs a summary, read_when and heading")
    fields = {key: value.strip() for key, value in match.groupdict().items()}
    if not all(fields.values()):
        raise ValueError(f"Documentation topic {path.stem!r} has an empty field")
    return dict(topic=path.stem, **fields)


def lookup(topic=None):
    """List topics, or return the metadata and Markdown for one topic."""
    if not TOPICS.is_dir() or TOPICS.resolve() != TOPICS:
        raise ValueError("The bundled AGI docs are unavailable")
    if topic is not None:
        if isinstance(topic, str) and SLUG.fullmatch(topic):
            path = TOPICS / f"{topic}.md"
            if path.exists():
                return dict(version=__version__, **_read_topic(path))
        raise ValueError("Unknown AGI docs topic. Use agi docs or chat_help without a topic to list them.")

    topics = []
    for path in sorted(TOPICS.glob("*.md")):
        if not SLUG.fullmatch(path.stem):
            raise ValueError(f"Invalid AGI docs topic name {path.stem!r}")
        fields = _read_topic(path)
        del fields["markdown"]
        topics.append(fields)
    return dict(version=__version__, topics=topics)


def release_notes(reader):
    """Show the topic index alongside unread notes, with the existing release receipt."""
    release = updates.unread_release(reader)
    result = dict(release=release)
    if release is not None:
        result["docs"] = lookup()
    return result


def format_cli(topic=None):
    result = lookup(topic)
    header = f"AGI {result['version']}"
    if topic is not None:
        return f"{header}\n\n{result['markdown']}"
    topics = "\n".join(f"{item['topic']} — {item['title']}: {item['read_when']}" for item in result["topics"])
    return f"{header}\n\n{topics}\n\nRun agi docs TOPIC to read a page."
