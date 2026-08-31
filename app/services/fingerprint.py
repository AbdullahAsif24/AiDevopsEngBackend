"""Build a compact repo fingerprint — the ONLY artifact sent to the model.

We never send the whole repo to Groq: that would blow the context window and
leak irrelevant files. Instead we curate a small bundle (see RepoFingerprint).

Why each filtering rule exists (the non-obvious part):
  * Skip dependency/build/output dirs (node_modules, .git, dist, build,
    __pycache__, .venv) — they're huge, machine-generated, and tell the model
    nothing about how to run the app.
  * Keep manifest files verbatim — they define dependencies, scripts, and the
    runtime, which is exactly what a Dockerfile needs.
  * Guess the entry point from package.json `main` or common conventional names,
    and include its content — it reveals the port and how the server starts.
  * Reuse any existing Dockerfile/compose so the model can learn from an
    author's intent instead of starting from scratch.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from ..contracts import EntryPoint, RepoFingerprint

# Directory names excluded from the file tree. Also always excluded: any path
# containing these segments, so nested node_modules etc. are dropped too.
_EXCLUDED_DIRS = {
    "node_modules", ".git", "dist", "build", "__pycache__", ".venv", "venv",
}

# Manifest files we include in FULL, plus well-known alt spellings.
_MANIFESTS = [
    "package.json",
    "requirements.txt",
    "pyproject.toml",
]

# Conventional entry-point guesses, in priority order (only used if no manifest).
_ENTRY_GUESSES = [
    "server.js", "index.js", "app.js", "main.js",
    "app.py", "main.py", "wsgi.py", "manage.py",
]

# Config files we surface text if present.
_EXISTING_DOCKERFILE = {"Dockerfile", "Containerfile"}
_COMPOSE_FILES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}


def _is_excluded(*path_parts: str) -> bool:
    """Return True if the given path touches any excluded directory segment."""
    # Note: a *file* named e.g. "dist" at root shouldn't be excluded, only
    # directories — but for our purposes any path whose segment matches an
    # excluded name is machine-generated noise, so we filter on segments.
    return any(seg in _EXCLUDED_DIRS for seg in path_parts)


def _safe_read(base: str, rel: str) -> Optional[str]:
    """Read a file as UTF-8, tolerating binary/unreadable content."""
    try:
        with open(os.path.join(base, rel), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, IsADirectoryError):
        return None


def _guess_entry_point(repo_root: str, manifests: dict[str, str]) -> Optional[EntryPoint]:
    """Best-effort guess at the app entry point + a snippet of its content.

    1. If package.json declares a `main`, but the more useful signal for a server
       is the `start` script — try resolving that file first.
    2. Otherwise try conventional filenames in priority order.
    3. Return the first file that actually exists, with its full content.
    """

    def try_file(rel: str) -> Optional[EntryPoint]:
        if not rel:
            return None
        content = _safe_read(repo_root, rel)
        if content is not None:
            return EntryPoint(path=rel, content=content)
        return None

    pkg = manifests.get("package.json")
    if pkg:
        try:
            data = json.loads(pkg)
        except json.JSONDecodeError:
            data = {}
        # `main` points at the module entry; best to prefer it for Node.
        main_rel = data.get("main")
        if main_rel:
            found = try_file(main_rel)
            if found:
                return found

    for rel in _ENTRY_GUESSES:
        found = try_file(rel)
        if found:
            return found

    return EntryPoint()


def build_fingerprint(repo_root: str, repo_url: str) -> RepoFingerprint:
    """Walk a cloned repo and assemble its RepoFingerprint contract."""
    file_tree: list[str] = []
    manifests: dict[str, str] = {}

    existing_dockerfile: Optional[str] = None
    existing_compose: Optional[str] = None

    # Recursively walk the tree using os.walk, which prunes naturally.
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Filter out excluded directories in-place so os.walk won't descend.
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_DIRS
        ]

        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, repo_root).replace("\\", "/")  # always use '/'
            parts = rel.split("/")

            # Skip anything whose path touches an excluded directory.
            if _is_excluded(*parts):
                continue

            file_tree.append(rel)

            # Capture manifests in full.
            if rel in _MANIFESTS:
                content = _safe_read(repo_root, rel)
                if content is not None:
                    manifests[rel] = content

            # Capture existing Dockerfile / compose text.
            if filename in _EXISTING_DOCKERFILE and existing_dockerfile is None:
                existing_dockerfile = _safe_read(repo_root, rel)
            if filename in _COMPOSE_FILES and existing_compose is None:
                existing_compose = _safe_read(repo_root, rel)

    # Sort the tree for deterministic output (stable across runs / diffs).
    file_tree.sort()

    entry_point = _guess_entry_point(repo_root, manifests)

    return RepoFingerprint(
        repo_url=repo_url,
        file_tree=file_tree,
        manifests=manifests,
        entry_point=entry_point,
        existing_dockerfile=existing_dockerfile,
        existing_compose=existing_compose,
    )
