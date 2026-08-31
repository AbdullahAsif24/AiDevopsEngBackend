"""GitHub URL validation + optional repo existence/size check."""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Matches github.com/<owner>/<repo>. The repo may optionally be followed by
# more path segments (e.g. a subfolder) or .git, which we tolerate but ignore.
_GITHUB_RE = re.compile(r"^github\.com[/\\](?P<owner>[^/\\]+)[/\\](?P<repo>[^/\\]+?)(?:\.git)?$", re.IGNORECASE)


class InvalidRepoURL(Exception):
    """Raised when a submitted URL is not a valid github.com/owner/repo URL."""


def parse_github_url(repo_url: str) -> tuple[str, str]:
    """Validate a GitHub URL and return (owner, repo).

    Accepts forms like:
      https://github.com/owner/repo
      http://github.com/owner/repo.git
      git@github.com:owner/repo.git   (SSH form -> normalized)
      git+https://github.com/owner/repo

    Anything else raises InvalidRepoURL. This is the gate for POST /jobs.
    """
    raw = repo_url.strip()

    # Normalize common prefixes so the regex can be simple.
    # SSH form: git@github.com:owner/repo.git
    ssh_match = re.match(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?$", raw)
    if ssh_match:
        return ssh_match.group("owner"), ssh_match.group("repo")

    # Strip explicit transport prefixes (git://, git+https://, https://, http://).
    without_transport = re.sub(r"^(git\+)?(https?|git|ssh)://", "", raw)
    m = _GITHUB_RE.match(without_transport)
    if not m:
        raise InvalidRepoURL(
            f"'{repo_url}' is not a valid github.com/owner/repo URL"
        )
    return m.group("owner"), m.group("repo")


def normalize_clone_url(repo_url: str) -> str:
    """Build a canonical https clone URL from the raw user input.

    We always clone over https (no auth needed for public repos) so the GitHub
    existence/size check and the clone target the same place.
    """
    owner, repo = parse_github_url(repo_url)
    return f"https://github.com/{owner}/{repo}.git"


def github_api_url(repo_url: str) -> str:
    """Return the GitHub API endpoint for metadata about this repo."""
    owner, repo = parse_github_url(repo_url)
    return f"https://api.github.com/repos/{owner}/{repo}"
