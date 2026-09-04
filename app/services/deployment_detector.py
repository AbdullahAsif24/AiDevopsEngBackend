"""Deployment-type detection: decide how a cloned repo should be deployed.

This module runs immediately after clone and before Dockerfile generation /
Vercel deploy branching. It classifies a repo into one of four buckets:

  * STATIC         -> pure frontend -> deploy straight to Vercel (no Dockerfile)
  * VERCEL_NATIVE  -> framework with a first-class Vercel build (Next.js, Nuxt,
                      SvelteKit/vercel adapter, ...) -> no Dockerfile
  * CONTAINER      -> backend that needs a Dockerfile -> Render/Railway
  * AMBIGUOUS      -> couldn't classify confidently -> manual review

Detection is two-tier:
  1. `detect_by_rules` -- a fast, free, deterministic scan for ~90% of repos.
  2. `detect_by_llm`   -- LLM fallback only when the rules return None.

The LLM provider here is Groq (the same client the Dockerfile generation uses);
we deliberately reuse ``groq_client._query_groq`` instead of duplicating the API
setup so there is a single place that owns the API key / model / error wrapping.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from ..contracts import (
    DetectionResult,
    DeploymentType,
)
from .groq_client import GroqCallError, _query_groq

# ---------------------------------------------------------------------------
# File-scope helpers / constants
# ---------------------------------------------------------------------------

# Directories excluded from the LLM file-tree context (machine generated/huge).
_EXCLUDED_DIRS = {
    "node_modules", ".git", "venv", ".venv", "dist", "build", "__pycache__",
}

# Manifest files whose raw contents we surface to the LLM.
_MANIFESTS = [
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "pom.xml",
    "build.gradle",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "vite.config.js",
    "vite.config.mjs",
    "vite.config.ts",
    "angular.json",
]

# Max chars of any single file sent to the LLM (budget control).
_MAX_FILE_CHARS = 4000

# Framework label for the "unknown / could not detect" LLM fallback path.
_UNKNOWN = "unknown"


def _safe_read_json(path: str) -> Optional[dict]:
    """Read a JSON manifest, returning None if missing/unparseable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, IsADirectoryError, json.JSONDecodeError):
        return None


def _read_file(repo_root: str, rel: str) -> Optional[str]:
    """Read a file as text, tolerating binary/unreadable content."""
    try:
        with open(os.path.join(repo_root, rel), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, IsADirectoryError):
        return None


def _dep_in(pkg: dict, name: str, *sections: str) -> bool:
    """True if `name` appears in any of the given package.json dep sections."""
    for section in sections:
        deps = pkg.get(section)
        if isinstance(deps, dict) and name in deps:
            return True
    return False


def _any_file_exists(repo_root: str, rels: list[str]) -> Optional[str]:
    """Return the first relative path that exists under repo_root, else None."""
    for rel in rels:
        if os.path.exists(os.path.join(repo_root, rel)):
            return rel
    return None


def _scan_files(repo_root: str) -> set[str]:
    """Walk the repo (skipping excluded dirs) and return posix relative paths."""
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, repo_root).replace("\\", "/")
            if any(seg in _EXCLUDED_DIRS for seg in rel.split("/")):
                continue
            found.add(rel)
    return found


def _contains_str(content: str, *needles: str) -> bool:
    """True if any needle appears in content (case-sensitive, cheap)."""
    return any(needle in content for needle in needles)


# ---------------------------------------------------------------------------
# Port extraction (regex over the entry point / config files)
# ---------------------------------------------------------------------------

_PORT_NODE = re.compile(r"\.listen\(\s*(\d+)")
_PORT_FLASK = re.compile(r"app\.run\(.*?port\s*=\s*(\d+)", re.IGNORECASE)
_PORT_SPRING_INLINE = re.compile(r"server\.port\s*=\s*(\d+)")
_PORT_YML = re.compile(r"port:\s*(\d+)")


def _detect_port(repo_root: str, entry_point: Optional[str], framework: str) -> Optional[int]:
    """Best-effort port detection. Never guesses a port -- returns None if unknown.

    Scans the entry point (and for Spring, server config files) for common
    patterns. A missing/unclear port is fine; the caller leaves it null rather
    than inventing a number.
    """
    if entry_point:
        content = _read_file(repo_root, entry_point)
        if content:
            if "flask" in framework.lower() or "django" in framework.lower():
                m = _PORT_FLASK.search(content)
            elif "spring" in framework.lower():
                m = _PORT_SPRING_INLINE.search(content)
            else:
                m = _PORT_NODE.search(content)
            if m:
                try:
                    return int(m.group(1))
                except (ValueError, IndexError):
                    return None

    # Spring Boot: check application.properties / application.yml for server.port.
    if "spring" in framework.lower():
        for rel in (
            "src/main/resources/application.properties",
            "src/main/resources/application.yml",
            "application.properties",
            "application.yml",
        ):
            content = _read_file(repo_root, rel)
            if not content:
                continue
            m = _PORT_SPRING_INLINE.search(content)
            if not m:
                m = _PORT_YML.search(content)
            if m:
                try:
                    return int(m.group(1))
                except (ValueError, IndexError):
                    return None
    return None


# ---------------------------------------------------------------------------
# 1. Rule-based detection
# ---------------------------------------------------------------------------

def _detect_entry_point(repo_root: str, pkg: Optional[dict]) -> Optional[str]:
    """Pick the Node/backend entry point (main field, or conventional names)."""
    if pkg:
        main = pkg.get("main")
        if main and os.path.exists(os.path.join(repo_root, main)):
            return main.replace("\\", "/")
    return _any_file_exists(
        repo_root, ["server.js", "index.js", "app.js", "main.js", "app.ts", "index.ts"]
    )


def detect_by_rules(repo_path: str) -> Optional[DetectionResult]:
    """Scan a cloned repo and return a confident rule-based result or None.

    Returns None when no rule matches confidently, causing the caller to fall
    through to LLM detection. Implements the exact priority order documented in
    the task (see module docstring / inline comments).
    """
    repo_root = os.path.abspath(repo_path)
    if not os.path.isdir(repo_root):
        return None

    vercel_json = os.path.join(repo_root, "vercel.json")
    pkg_path = os.path.join(repo_root, "package.json")
    pkg = _safe_read_json(pkg_path)

    # a) vercel.json at repo root -> native Vercel deploy.
    if os.path.exists(vercel_json):
        return DetectionResult(
            deployment_type=DeploymentType.VERCEL_NATIVE,
            confidence="high",
            detected_framework="Vercel",
            reasoning="vercel.json found at repo root; Vercel-native project.",
            needs_dockerfile=False,
            detection_method="rule_based",
        )

    # b) package.json with "next" -> Next.js, native Vercel.
    if pkg is not None and _dep_in(pkg, "next", "dependencies", "devDependencies"):
        return DetectionResult(
            deployment_type=DeploymentType.VERCEL_NATIVE,
            confidence="high",
            detected_framework="Next.js",
            entry_point=_detect_entry_point(repo_root, pkg),
            reasoning="package.json declares 'next'; Next.js deploys natively to Vercel.",
            needs_dockerfile=False,
            detection_method="rule_based",
        )

    # c) Nuxt with vercel preset, or SvelteKit vercel adapter -> native Vercel.
    if pkg is not None:
        nuxt_preset = False
        for rel in ("nuxt.config.ts", "nuxt.config.js", "nuxt.config.mjs"):
            content = _read_file(repo_root, rel)
            if content and _contains_str(content, "vercel"):
                nuxt_preset = True
                break
        nuxt_dep = _dep_in(pkg, "nuxt", "dependencies", "devDependencies")
        svelte_vercel = _dep_in(pkg, "@sveltejs/adapter-vercel", "dependencies", "devDependencies")
        if (nuxt_dep and nuxt_preset) or svelte_vercel:
            return DetectionResult(
                deployment_type=DeploymentType.VERCEL_NATIVE,
                confidence="high",
                detected_framework="Nuxt" if nuxt_dep else "SvelteKit",
                entry_point=_detect_entry_point(repo_root, pkg),
                reasoning="Nuxt with Vercel preset or SvelteKit vercel adapter; deploys natively.",
                needs_dockerfile=False,
                detection_method="rule_based",
            )

    # d) CRA (react-scripts) or Vite with no server entry -> static frontend.
    if pkg is not None:
        is_cra = _dep_in(pkg, "react-scripts", "dependencies", "devDependencies")
        has_vite = _dep_in(pkg, "vite", "dependencies", "devDependencies")
        if is_cra or has_vite:
            server_entry = _detect_entry_point(repo_root, pkg)
            if not server_entry:
                return DetectionResult(
                    deployment_type=DeploymentType.STATIC,
                    confidence="high",
                    detected_framework="Create React App" if is_cra else "Vite",
                    reasoning="react-scripts (CRA) or Vite present with no server entry; pure static build.",
                    needs_dockerfile=False,
                    detection_method="rule_based",
                )

    # e) angular.json -> Angular SPA (static).
    if os.path.exists(os.path.join(repo_root, "angular.json")):
        return DetectionResult(
            deployment_type=DeploymentType.STATIC,
            confidence="high",
            detected_framework="Angular",
            reasoning="angular.json present; Angular builds to a static bundle.",
            needs_dockerfile=False,
            detection_method="rule_based",
        )

    # f) Static site generators.
    if os.path.exists(os.path.join(repo_root, "_config.yml")):
        return DetectionResult(
            deployment_type=DeploymentType.STATIC,
            confidence="high",
            detected_framework="Jekyll",
            reasoning="_config.yml present; Jekyll static site.",
            needs_dockerfile=False,
            detection_method="rule_based",
        )
    if _any_file_exists(repo_root, ["astro.config.js", "astro.config.mjs", "astro.config.ts"]):
        return DetectionResult(
            deployment_type=DeploymentType.STATIC,
            confidence="high",
            detected_framework="Astro",
            reasoning="astro.config.* present; Astro static site.",
            needs_dockerfile=False,
            detection_method="rule_based",
        )
    hugo_toml = os.path.join(repo_root, "hugo.toml")
    if os.path.exists(hugo_toml):
        content = _read_file(repo_root, "hugo.toml") or ""
        if _contains_str(content, "baseURL") or _contains_str(content, "title"):
            return DetectionResult(
                deployment_type=DeploymentType.STATIC,
                confidence="high",
                detected_framework="Hugo",
                reasoning="hugo.toml present with Hugo markers; static site.",
                needs_dockerfile=False,
                detection_method="rule_based",
            )
    config_toml = os.path.join(repo_root, "config.toml")
    if os.path.exists(config_toml):
        content = _read_file(repo_root, "config.toml") or ""
        if _contains_str(content, "baseURL") or _contains_str(content, "theme"):
            return DetectionResult(
                deployment_type=DeploymentType.STATIC,
                confidence="high",
                detected_framework="Hugo",
                reasoning="config.toml present with Hugo markers; static site.",
                needs_dockerfile=False,
                detection_method="rule_based",
            )

    # g) Python backend signals.
    py_framework = None
    for rel in ("requirements.txt", "pyproject.toml"):
        content = _read_file(repo_root, rel)
        if not content:
            continue
        for name in ("flask", "fastapi", "django"):
            if name in content:
                py_framework = name
                break
        if py_framework:
            break

    if py_framework:
        entry_point = _detect_python_entry(repo_root, py_framework)
        port = _detect_port(repo_root, entry_point, py_framework)
        return DetectionResult(
            deployment_type=DeploymentType.CONTAINER,
            confidence="high",
            detected_framework={
                "flask": "Flask", "fastapi": "FastAPI", "django": "Django",
            }[py_framework],
            entry_point=entry_point,
            listen_port=port,
            reasoning=f"{py_framework} detected in Python manifests; needs a container.",
            needs_dockerfile=True,
            detection_method="rule_based",
        )

    # h) Java Spring Boot backend.
    java_build = _any_file_exists(repo_root, ["pom.xml", "build.gradle"])
    if java_build:
        spring_class = _find_spring_boot_class(repo_root)
        if spring_class:
            port = _detect_port(repo_root, spring_class, "spring")
            return DetectionResult(
                deployment_type=DeploymentType.CONTAINER,
                confidence="high",
                detected_framework="Spring Boot",
                entry_point=spring_class,
                listen_port=port,
                reasoning="Java build file with @SpringBootApplication found; needs a container.",
                needs_dockerfile=True,
                detection_method="rule_based",
            )

    # i) Node backend (non-Vercel): express/fastify/koa/@nestjs/core.
    if pkg is not None:
        backend = _dep_in(pkg, "express", "dependencies") or _dep_in(pkg, "fastify", "dependencies") \
            or _dep_in(pkg, "koa", "dependencies") or _dep_in(pkg, "@nestjs/core", "dependencies")
        if backend:
            has_vercel_adapter = (
                _dep_in(pkg, "@sveltejs/adapter-vercel", "dependencies", "devDependencies")
                or _dep_in(pkg, "vercel", "dependencies", "devDependencies")
            )
            if not has_vercel_adapter:
                entry_point = _detect_entry_point(repo_root, pkg)
                port = _detect_port(repo_root, entry_point, "node")
                return DetectionResult(
                    deployment_type=DeploymentType.CONTAINER,
                    confidence="high",
                    detected_framework="Node.js",
                    entry_point=entry_point,
                    listen_port=port,
                    reasoning="Node backend framework present (express/fastify/koa/nest) with no Vercel adapter.",
                    needs_dockerfile=True,
                    detection_method="rule_based",
                )

    # j) No confident match -> fall through to LLM.
    return None


def _detect_python_entry(repo_root: str, framework: str) -> Optional[str]:
    """Find the Python entry point for a known framework."""
    check_rels = [] if framework == "django" else [
        "app.py", "main.py", "app/app.py", "src/app.py", "application.py",
    ]

    if framework == "fastapi" or framework == "flask":
        for rel in check_rels:
            content = _read_file(repo_root, rel)
            if content and _contains_str(content, "app = Flask(", "app = FastAPI("):
                return rel

    if framework == "django" or _read_file(repo_root, "manage.py") is not None:
        if os.path.exists(os.path.join(repo_root, "manage.py")):
            return "manage.py"
        return _any_file_exists(repo_root, ["manage.py", "wsgi.py", "asgi.py"])

    # Fallback: wsgi/asgi.
    return _any_file_exists(repo_root, ["wsgi.py", "asgi.py", "app.py", "main.py"])


def _find_spring_boot_class(repo_root: str) -> Optional[str]:
    """Locate the file containing @SpringBootApplication (returns relative path)."""
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for filename in filenames:
            if not filename.endswith(".java"):
                continue
            full = os.path.join(dirpath, filename)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    if "@SpringBootApplication" in f.read():
                        return os.path.relpath(full, repo_root).replace("\\", "/")
            except OSError:
                continue
    return None


# ---------------------------------------------------------------------------
# 2. LLM fallback detection
# ---------------------------------------------------------------------------

# The exact classification prompt handed to the LLM. It demands strict JSON so we
# can parse the result back into a DetectionResult.
CLASSIFICATION_PROMPT = """\
You are an expert DevOps/platform engineer. Given a compact fingerprint of a \
GitHub repository, classify how it should be deployed.

Return ONLY a single JSON object with EXACTLY these keys:
{
  "deployment_type": "static" | "vercel_native" | "container" | "ambiguous",
  "confidence": "high" | "medium" | "low",
  "detected_framework": "string, e.g. Next.js, Vite, Flask, Spring Boot, or unknown",
  "entry_point": "relative file path or null",
  "listen_port": "integer or null",
  "reasoning": "short human-readable justification",
  "needs_dockerfile": true | false,
  "ambiguous_reason": "string or null (only when deployment_type is ambiguous)"
}

## RULES
- "static": pure frontend / static site (plain HTML, Vite/CRA/Astro build output, \
Angular) that deploys directly to Vercel. No server code.
- "vercel_native": a framework Vercel builds natively (Next.js, Nuxt with Vercel \
preset, SvelteKit with @sveltejs/adapter-vercel, Remix if applicable). Deploys to \
Vercel without a Dockerfile. needs_dockerfile = false.
- "container": a backend (Flask/FastAPI/Django, Express/Fastify/Koa/NestJS, Spring \
Boot, etc.) that needs a Dockerfile, OR a Node backend with no Vercel adapter. \
needs_dockerfile = true.
- "ambiguous": you cannot confidently classify. Provide ambiguous_reason.
- NEVER invent an entry_point or listen_port you did not see. Leave null if unknown.
- Listen port: only report if the entry point or config file shows it (e.g. \
app.listen(PORT), app.run(port=...), server.port=...). Do NOT guess defaults.

## REPO FILE TREE
{file_tree}

## MANIFEST CONTENTS
{manifests}

Return ONLY the JSON object now — no markdown fences, no prose.
"""


def _build_context(repo_path: str) -> tuple[list[str], str]:
    """Build (file_tree, manifests_text) for the LLM prompt.

    * file_tree       -> all relative paths (top 3 levels), excluding big dirs.
    * manifests_text  -> raw contents of known manifests + existing Dockerfile,
                         each truncated to _MAX_FILE_CHARS.
    """
    repo_root = os.path.abspath(repo_path)
    files = sorted(_scan_files(repo_root))

    # Keep only top 3 path levels for brevity in the tree.
    tree_max_depth: list[str] = []
    for rel in files:
        parts = rel.split("/")
        if len(parts) <= 3:
            tree_max_depth.append(rel)
    file_tree = tree_max_depth

    manifest_blocks: list[str] = []
    for rel in files:
        if rel in _MANIFESTS or rel == "Dockerfile":
            content = _read_file(repo_root, rel)
            if content is None:
                continue
            truncated = content[:_MAX_FILE_CHARS]
            manifest_blocks.append(f"--- {rel} ---\n{truncated}")

    manifests_text = "\n\n".join(manifest_blocks) if manifest_blocks else "(no manifests found)"
    return file_tree, manifests_text


def _strip_json_fences(raw: str) -> str:
    """Strip ```json ... ``` fences (and surrounding whitespace) if present."""
    text = raw.strip()
    if text.startswith("```"):
        # Drop leading fence line(s).
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # Drop trailing fence.
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_llm_result(raw: str) -> Optional[dict]:
    """Parse LLM JSON output, handling markdown fences + retry once."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # First retry: strip ```json fences.
    stripped = _strip_json_fences(raw)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _llm_to_result(data: dict) -> DetectionResult:
    """Coerce a parsed LLM dict into a validated DetectionResult.

    Never raises: any malformed field falls back to a safe default so a weird
    model response degrades to AMBIGUOUS rather than crashing the pipeline.
    """
    try:
        deployment_type = DeploymentType(
            str(data.get("deployment_type")) or DeploymentType.AMBIGUOUS.value
        )
    except ValueError:
        deployment_type = DeploymentType.AMBIGUOUS

    confidence = str(data.get("confidence", "low"))
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    entry_point = data.get("entry_point")
    entry_point = str(entry_point) if isinstance(entry_point, str) and entry_point else None

    raw_port = data.get("listen_port")
    listen_port = None
    if isinstance(raw_port, (int, float)) and not isinstance(raw_port, bool):
        listen_port = int(raw_port)

    ambiguous_reason = data.get("ambiguous_reason")
    ambiguous_reason = str(ambiguous_reason) if ambiguous_reason else None

    return DetectionResult(
        deployment_type=deployment_type,
        confidence=confidence,
        detected_framework=str(data.get("detected_framework", "") or _UNKNOWN),
        entry_point=entry_point,
        listen_port=listen_port,
        reasoning=str(data.get("reasoning", "") or ""),
        needs_dockerfile=bool(
            data.get("needs_dockerfile", deployment_type == DeploymentType.CONTAINER)
        ),
        ambiguous_reason=ambiguous_reason,
        detection_method="llm",
    )


async def detect_by_llm(repo_path: str, file_tree: str) -> DetectionResult:
    """LLM fallback classifier. Only called when detect_by_rules() returns None.

    Builds a compact context payload from the repo (top-3-level tree plus raw
    manifest contents), calls the shared Groq client, and parses the JSON result.
    On any failure (network, timeout, bad parse) returns an AMBIGUOUS
    DetectionResult so the pipeline never crashes.
    """
    repo_root = os.path.abspath(repo_path)
    _, manifests_text = _build_context(repo_root)

    prompt = (
        CLASSIFICATION_PROMPT
        .replace("{file_tree}", file_tree or "(empty tree)")
        .replace("{manifests}", manifests_text)
    )

    try:
        raw = await _query_groq(prompt)
    except GroqCallError as exc:
        return DetectionResult(
            deployment_type=DeploymentType.AMBIGUOUS,
            confidence="low",
            detected_framework=_UNKNOWN,
            reasoning="LLM detection failed",
            needs_dockerfile=False,
            ambiguous_reason=(
                f"Detection service unavailable, manual classification required: {exc}"
            ),
            detection_method="llm",
        )

    data = _parse_llm_result(raw)
    if data is None:
        return DetectionResult(
            deployment_type=DeploymentType.AMBIGUOUS,
            confidence="low",
            detected_framework=_UNKNOWN,
            reasoning="LLM response could not be parsed",
            needs_dockerfile=False,
            ambiguous_reason="LLM response could not be parsed",
            detection_method="llm",
        )

    return _llm_to_result(data)


# ---------------------------------------------------------------------------
# 3. Orchestrator
# ---------------------------------------------------------------------------

async def detect_deployment_type(repo_path: str) -> DetectionResult:
    """Top-level entry point. Rules first; LLM only as an unambiguous fallback.

    * Fast, free, deterministic rules first (skips the LLM entirely for ~90% of
      repos -- saves API cost and latency).
    * If rules return None, falls through to the LLM classifier.
    * Wrapped in a broad try/except so one bad repo can never kill the job queue.

    Logs which method was used for observability during the hackathon demo.
    """
    import logging
    logger = logging.getLogger("aidevops.detector")

    try:
        if not os.path.isdir(repo_path):
            raise ValueError(f"repo path does not exist or is empty: {repo_path}")

        # Fast, deterministic path (~90% of repos).
        rule_based = detect_by_rules(repo_path)
        if rule_based is not None:
            logger.info("detection via rule_based: %s", rule_based.deployment_type)
            return rule_based

        # Fallback only reached when rules couldn't classify.
        logger.info("rules inconclusive; falling through to LLM detection")
        file_tree = "\n".join(_build_context(repo_path)[0])
        result = await detect_by_llm(repo_path, file_tree)
        logger.info("detection via llm: %s", result.deployment_type)
        return result
    except Exception as exc:  # pragma: no cover - broad safety net
        logger.error("Deployment detection failed: %s", exc)
        return DetectionResult(
            deployment_type=DeploymentType.AMBIGUOUS,
            confidence="low",
            detected_framework=_UNKNOWN,
            reasoning="Detection failed",
            needs_dockerfile=False,
            ambiguous_reason=(
                "Detection service unavailable, manual classification required: "
                + str(exc)
            ),
            detection_method="rule_based",
        )