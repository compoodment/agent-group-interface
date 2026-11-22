"""Check explicitly named tags on the two public download repositories."""

from functools import lru_cache
import json
import time
from urllib.parse import quote
from urllib.request import Request, urlopen


REPOSITORIES = {"AGI": "compoodment/agent-group-interface", "ArtLab": "compoodment/Artlab"}
POLL_SECONDS = 120
MAX_RESPONSE = 128 * 1024


@lru_cache(maxsize=64)
def _published(project, tag, _interval):
    repo = REPOSITORIES[project]
    url = f"https://api.github.com/repos/{repo}/releases/tags/{quote(tag, safe='')}"
    request = Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urlopen(request, timeout=3) as response:
            payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            return False
        release = json.loads(payload)
    except (OSError, ValueError):
        return False
    return (isinstance(release, dict) and release.get("tag_name") == tag and release.get("draft") is False
            and isinstance(release.get("published_at"), str) and bool(release["published_at"]))


def published(project, tag):
    return _published(project, tag, int(time.monotonic() // POLL_SECONDS))
