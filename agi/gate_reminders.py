"""Exact-commit checks for the two local integration gates."""

from functools import lru_cache
import os
import re
import subprocess
from pathlib import Path


FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
REPOSITORIES = {
    "AGI": Path(os.environ.get("AGI_GATE_REPO") or Path(__file__).resolve().parents[1]).expanduser(),
    # The key is embedded in immutable reminder events.
    "ArtLab": Path(repo).expanduser() if (repo := os.environ.get("ARTLAB_GATE_REPO")) else None,
}


def display_name(project):
    return "Artlab" if project == "ArtLab" else project


def project_id(label):
    if isinstance(label, str):
        for project in REPOSITORIES:
            if label.casefold() == project.casefold():
                return project
    raise ValueError("Project must be AGI or Artlab")


def _project_name(project):
    if not isinstance(project, str) or project not in REPOSITORIES:
        raise ValueError("Project must be AGI or Artlab")
    return project


def _repository(project):
    _project_name(project)
    repo = REPOSITORIES[project]
    if repo is None or not repo.is_dir():
        raise ValueError(f"The local {display_name(project)} repository is unavailable")
    return repo


def validate_target_format(project, commit):
    """Validate the project selector and exact revision shape without running Git."""
    _project_name(project)
    if not isinstance(commit, str) or FULL_COMMIT.fullmatch(commit) is None:
        raise ValueError("Commit must be a full 40-character lowercase SHA-1")
    return project, commit


def validate_commit(project, commit):
    """Require a full, lowercase SHA-1 naming a commit in the selected local repository."""
    project, commit = validate_target_format(project, commit)
    repo = _repository(project)
    try:
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
                                 f"{commit}^{{commit}}"], capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Commit is not available in the local {display_name(project)} repository") from error
    if result.stdout.strip() != commit:
        raise ValueError("Commit must identify the exact full revision")
    return commit


def main_head(project):
    """Read the loose main ref cheaply and let Git handle alternate ref storage."""
    repo = _repository(project)
    try:
        with (repo / ".git/refs/heads/main").open("rb") as ref:
            direct = ref.read(64).strip().decode("ascii")
    except (OSError, UnicodeError):
        direct = ""
    if FULL_COMMIT.fullmatch(direct):
        return direct
    try:
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/main^{commit}"],
                                capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError(f"Could not read {display_name(project)} main") from error
    head = result.stdout.strip()
    if FULL_COMMIT.fullmatch(head) is None:
        raise OSError(f"Invalid {display_name(project)} main revision")
    return head


def is_ancestor(project, commit, head):
    """Whether the exact candidate commit is reachable from the captured main head."""
    repo = _repository(project)
    if FULL_COMMIT.fullmatch(commit) is None or FULL_COMMIT.fullmatch(head) is None:
        raise ValueError("Commit and main head must be full SHA-1 revisions")
    return _is_ancestor(repo, commit, head)


@lru_cache(maxsize=128)
def _is_ancestor(repo, commit, head):
    try:
        result = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, head],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError(f"Could not check main history in {repo.name}") from error
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise OSError(f"Could not check main history in {repo.name}")
