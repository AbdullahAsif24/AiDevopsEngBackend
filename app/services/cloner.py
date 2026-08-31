"""Shallow repo cloning with per-job temp directory lifecycle.

Key rules for concurrency safety (this is a hot spot):
  * clone_from() is a blocking/CPU+IO operation, so it runs in a threadpool.
  * Every job gets its OWN tempfile.mkdtemp() directory — never shared.
  * The temp dir is cleaned up (shutil.rmtree) when the job finishes, success
    or failure. The caller owns the cleanup via a context manager.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from typing import Optional

from git import Repo, GitCommandError

from .github import InvalidRepoURL, normalize_clone_url, github_api_url


class CloneError(Exception):
    """Raised when we cannot obtain the repo (not found, uncloneable, etc.)."""


async def check_repo_exists(repo_url: str, timeout: float = 10.0) -> None:
    """Optionally hit the GitHub API to confirm the repo exists and isn't huge.

    Running before a clone is a cheap gate: we avoid cloning something that
    doesn't exist (common typo), and we can bail on suspiciously large repos.
    Uses httpx in async mode so it doesn't block the event loop.

    We only *warn* on missing info (e.g. rate-limited) rather than hard-failing,
    because the API is a best-effort check. A 404 is a hard failure though.
    """
    import httpx

    url = github_api_url(repo_url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)

    if resp.status_code == 404:
        raise CloneError(f"Repo not found on GitHub: {repo_url}")
    if resp.status_code != 200:
        # Rate-limited or other transient error: let the clone itself decide.
        return


class RepoSnapshot:
    """A cloned repo living in a temp dir, with lifecycle management.

    Using a context manager guarantees cleanup on success OR failure:
        async with await clone_repo(url) as snapshot:
            ... fingerprint ...
        # temp dir already removed here.
    """

    def __init__(self, repo_url: str, path: str):
        self.repo_url = repo_url
        self.path = path
        self._temp_root: Optional[str] = None

    @property
    def root(self) -> str:
        """The directory containing the repo checkout (repo itself is a subdir).

        GitPython clones INTO the dir we give it, making that dir the repo root,
        so `path` IS the repo root we hand to the fingerprint builder.
        """
        return self.path

    async def cleanup(self) -> None:
        """Remove the temp directory. Called on success and failure alike.

        On Windows a freshly-closed GitPython repo can leave a `.git` entry
        momentarily locked, so rmtree may partially fail on the first try. We
        retry once after a short delay to give the file handle time to release.
        """
        if not (self._temp_root and os.path.isdir(self._temp_root)):
            return

        # rmtree is blocking; run in the threadpool to avoid stalling the loop.
        def _rm():
            for _ in range(3):
                try:
                    shutil.rmtree(self._temp_root)
                    return True
                except OSError:
                    # Wait briefly for Windows to release locked .git handles.
                    time.sleep(0.3)
            # Give up after retries, but don't crash the job.
            return False

        import asyncio

        await asyncio.get_running_loop().run_in_executor(None, _rm)
        self._temp_root = None

    async def __aenter__(self) -> "RepoSnapshot":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.cleanup()


async def clone_repo(repo_url: str, check_github: bool = True) -> RepoSnapshot:
    """Shallow-clone a GitHub repo into a fresh temp dir.

    1. Validate the URL / normalize it.
    2. Optionally hit the GitHub API (existence/size gate).
    3. Create a unique temp dir and shallow (depth=1) clone into it.

    Returns a RepoSnapshot context manager. Raises CloneError / InvalidRepoURL.
    """
    # Validate early so we never create a temp dir for a malformed URL.
    try:
        clone_url = normalize_clone_url(repo_url)
    except InvalidRepoURL as exc:
        raise InvalidRepoURL(str(exc)) from exc

    if check_github:
        await check_repo_exists(repo_url)

    # One mkdtemp per job — jobs never share a directory.
    workdir = tempfile.mkdtemp(prefix="aidevops_")

    def _do_clone():
        try:
            # depth=1 => shallow clone (no history) — plenty for analysis, fast.
            Repo.clone_from(clone_url, workdir, depth=1)
        except GitCommandError as exc:
            raise CloneError(f"Clone failed: {exc}") from exc

    # clone is blocking; run in threadpool so concurrent jobs don't serialize.
    # Wrap with a timeout so a hung clone (e.g. no network) can't stick a job
    # forever — the coroutine is released even if the OS thread lingers.
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(None, _do_clone), timeout=60.0)
    except asyncio.TimeoutError as exc:
        raise CloneError(f"Clone timed out for {repo_url}") from exc

    snapshot = RepoSnapshot(repo_url=repo_url, path=workdir)
    snapshot._temp_root = workdir
    return snapshot
