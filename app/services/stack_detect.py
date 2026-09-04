"""Detect Node server vs Vite/React SPA vs Python from a fingerprint."""
from __future__ import annotations

import json
from typing import Optional

from ..contracts import Framework, RepoFingerprint


def _pkg(fingerprint: RepoFingerprint) -> dict:
    raw = fingerprint.manifests.get("package.json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _deps(pkg: dict) -> set[str]:
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = pkg.get(key) or {}
        if isinstance(block, dict):
            out.update(str(k).lower() for k in block)
    return out


def is_vite_spa(fingerprint: RepoFingerprint) -> bool:
    """True for Vite/React (or similar) frontends that `npm run build` → dist/."""
    tree = {p.replace("\\", "/").lower() for p in fingerprint.file_tree}
    pkg = _pkg(fingerprint)
    deps = _deps(pkg)
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}

    has_vite_config = any(
        p.endswith("vite.config.ts")
        or p.endswith("vite.config.js")
        or p.endswith("vite.config.mts")
        for p in tree
    )
    has_vite_dep = "vite" in deps or "@vitejs/plugin-react" in deps
    has_build = "build" in scripts
    # Server frameworks — treat as node, not static.
    serverish = {
        "express",
        "fastify",
        "koa",
        "hapi",
        "@nestjs/core",
        "next",
        "nuxt",
    }
    if deps & serverish:
        return False
    if has_vite_config or has_vite_dep:
        return has_build or True
    # React CRA-style without express
    if "react" in deps and "react-scripts" in deps:
        return True
    return False


def guess_static_out_dir(fingerprint: RepoFingerprint) -> str:
    pkg = _pkg(fingerprint)
    deps = _deps(pkg)
    if "react-scripts" in deps:
        return "build"
    return "dist"


def detect_framework(fingerprint: RepoFingerprint) -> Framework:
    """Best-effort stack classification for template selection."""
    if fingerprint.manifests.get("requirements.txt") or fingerprint.manifests.get(
        "pyproject.toml"
    ):
        return Framework.PYTHON

    if is_vite_spa(fingerprint):
        return Framework.STATIC

    if fingerprint.manifests.get("package.json"):
        return Framework.NODE

    tree = " ".join(fingerprint.file_tree).lower()
    if "index.html" in tree:
        return Framework.STATIC

    return Framework.NODE


def recommended_port(framework: Framework) -> int:
    if framework == Framework.STATIC:
        return 80
    if framework == Framework.PYTHON:
        return 8000
    return 3000
