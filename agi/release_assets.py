"""Find verified AGI release archives in the project's GitHub repository."""

from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
from pathlib import PurePosixPath
import re
import shutil
import tarfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROJECT = "compoodment/agent-group-interface"
RELEASES = f"https://api.github.com/repos/{PROJECT}/releases"
DOWNLOADS = f"https://github.com/{PROJECT}/releases/download"
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-alpha\.(0|[1-9][0-9]*))?")
DIGEST = re.compile(r"sha256:([a-f0-9]{64})")
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024
HOSTS = frozenset(("api.github.com", "github.com", "release-assets.githubusercontent.com",
                   "objects.githubusercontent.com"))


@dataclass(frozen=True)
class Release:
    version: str
    name: str
    url: str
    sha256: str
    size: int
    notes: str


def version_key(version):
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"Unsupported AGI version: {version}")
    major, minor, patch, alpha = match.groups()
    return int(major), int(minor), int(patch), alpha is None, int(alpha or 0)


def _check_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc not in HOSTS
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("The release redirected outside the expected HTTPS download hosts.")


class _ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _open(url):
    _check_url(url)
    request = Request(url, headers={"User-Agent": "AGI-updater", "Accept": "application/vnd.github+json"})
    return build_opener(_ReleaseRedirects()).open(request, timeout=20)


def _records(url):
    try:
        with _open(url) as response:
            content = response.read(4 * 1024 * 1024 + 1)
    except HTTPException as error:
        raise ValueError("The release service did not finish sending its response. Try again.") from error
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("Release metadata is too large.")
    return json.loads(content)


def find(installed, requested=None):
    current = version_key(installed)
    if requested is not None:
        version_key(requested)
        records = [_records(f"{RELEASES}/tags/v{requested}")]
    else:
        records = _records(f"{RELEASES}?per_page=100")
    if not isinstance(records, list):
        raise ValueError("GitHub returned invalid release metadata.")
    candidates = []
    for item in records:
        if not isinstance(item, dict) or item.get("draft") is not False or not item.get("published_at"):
            continue
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v") or not VERSION.fullmatch(tag[1:]):
            continue
        version = tag[1:]
        key = version_key(version)
        if key <= current or (requested is not None and version != requested):
            continue
        if requested is None and (key[3] != current[3] or item.get("prerelease") != (not current[3])):
            continue
        candidates.append((key, version, item))
    if not candidates:
        if requested is not None:
            raise ValueError(f"No published AGI release newer than {installed} matches {requested}.")
        return None

    _, version, record = max(candidates, key=lambda candidate: candidate[0])
    name = f"agent-group-interface-{version}.tar.gz"
    assets = record.get("assets")
    if not isinstance(assets, list):
        raise ValueError(f"Release {version} has no valid asset list.")
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matching) != 1:
        raise ValueError(f"Release {version} must publish one {name} archive.")
    asset = matching[0]
    url = f"{DOWNLOADS}/v{version}/{name}"
    digest = asset.get("digest")
    size = asset.get("size")
    if (asset.get("state") != "uploaded" or asset.get("browser_download_url") != url
            or not isinstance(digest, str) or not DIGEST.fullmatch(digest)
            or type(size) is not int or not 0 < size <= MAX_ARCHIVE):
        raise ValueError(f"Release {version} does not have a verifiable archive.")
    notes = record.get("body")
    return Release(version, name, url, digest[7:], size, notes if isinstance(notes, str) else "")


def download(release, path):
    digest = hashlib.sha256()
    size = 0
    try:
        with _open(release.url) as response, path.open("xb") as output:
            while data := response.read(128 * 1024):
                size += len(data)
                if size > release.size:
                    raise ValueError("The download is larger than the release record.")
                digest.update(data)
                output.write(data)
    except HTTPException as error:
        raise ValueError("The release download was interrupted. Try again.") from error
    if size != release.size or digest.hexdigest() != release.sha256:
        raise ValueError("The download does not match the release checksum.")


def unpack(release, path, destination):
    root = release.name.removesuffix(".tar.gz")
    seen = set()
    total = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if (not name.parts or name.parts[0] != root or ".." in name.parts or name in seen
                        or len(seen) >= 4096 or not (member.isdir() or member.isreg())):
                    raise ValueError("Unexpected file in the AGI release archive.")
                seen.add(name)
                target = destination.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                total += member.size
                if total > MAX_UNPACKED:
                    raise ValueError("The AGI release archive expands beyond its supported size.")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.extractfile(member) as data, target.open("xb") as output:
                    shutil.copyfileobj(data, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except tarfile.TarError as error:
        raise ValueError("The downloaded AGI archive is unreadable.") from error
    return destination / root
