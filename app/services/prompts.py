"""Groq prompt templates — the two prompt variants used by the pipeline.

Two separate, reusable strings:
  1. GENERATION_PROMPT  -> initial analysis + Dockerfile generation
  2. PATCH_PROMPT        -> error-patch (self-heal) variant

Both demand STRICTLY JSON output (no preamble, no markdown fences) because we
feed `response_format={"type": "json_object"}` to Groq and validate with Pydantic.
Keeping them as separate module-level strings lets us tune each independently
without touching the agent logic.
"""
from __future__ import annotations

import json

from ..contracts import RepoFingerprint

# The JSON schema we expect the model to return, embedded in the prompt so the
# model knows the exact keys AND constraints (esp. framework enum + dockerfile).
_RESPONSE_SCHEMA = """{
  "language": "one of 'javascript', 'python', or 'html'",
  "framework": "one of 'node', 'python', 'static'",
  "entry_point": "relative path string or null",
  "port": "integer the app listens on, or null",
  "start_command": "shell command string or null",
  "dockerfile_content": "the COMPLETE filled-in Dockerfile as a string"
}"""


def _fingerprint_payload(fingerprint: RepoFingerprint) -> str:
    """Serialize the fingerprint to a compact JSON string for the prompt body."""
    return fingerprint.model_dump_json()


def build_generation_prompt(fingerprint: RepoFingerprint, template_skeletons: str) -> str:
    """Prompt for the FIRST pass: analyze the repo and produce a Dockerfile.

    `template_skeletons` is describe_templates() output — the three skeletons the
    model is allowed to fill. We forbid freeform Docker syntax, which bounds the
    output space and makes retries meaningful.
    """
    return f"""\
You are an expert DevOps engineer. Given a compact fingerprint of a GitHub repo,
produce a working Dockerfile by FILLING IN ONE of the provided template skeletons.
Do NOT write Dockerfile syntax from scratch — only fill placeholders.

## STRICT OUTPUT RULES
- Reply with ONLY a single JSON object. No preamble, no explanation, no
  markdown code fences (no ```), no trailing prose.
- The JSON must match exactly this schema:
{_RESPONSE_SCHEMA}

## REPO FINGERPRINT (JSON)
{_fingerprint_payload(fingerprint)}

## AVAILABLE TEMPLATES (choose the one matching the repo's stack)
{template_skeletons}

## GUIDANCE
- Detect the stack: Node/Express if package.json lists 'express' OR the file
  tree/entry point is a .js server. Python if requirements.txt / pyproject.toml
  exists and entry point is a .py app. Otherwise static (HTML/CSS/JS or dist/).
- Java / compiled languages are OUT OF SCOPE. If the repo doesn't clearly map to
  one of the three templates, choose the closest and note it in metadata.
- Pick the correct template, then substitute:
  * $FRAMEWORK_NOTE  -> short human description, e.g. "Express for Node", or
                        "FastAPI (Python)".
  * $PORT            -> the actual port from the fingerpint's entry point or
                        manifest scripts (default 3000 for node, 8000 for python).
  * $START_COMMAND   -> how to launch, e.g. "node server.js", or
                        "uvicorn main:app --host 0.0.0.0 --port $PORT".
  * $STATIC_SOURCE   -> for static: the directory to copy, usually 'dist' or '.'.
- In the returned JSON: `dockerfile_content` must be the COMPLETE Dockerfile
  (all lines, fully filled). `framework` = 'node' | 'python' | 'static'.
- If an existing Dockerfile/docker-compose was provided in the fingerprint, prefer
  adapting it (fixing obvious errors) rather than regenerating.

Return ONLY the JSON object now.
"""


def build_patch_prompt(
    fingerprint: RepoFingerprint,
    previous_dockerfile: str,
    build_error: str,
    template_skeletons: str,
    attempt: int,
) -> str:
    """Prompt for the SELF-HEAL pass: a build failed, patch the Dockerfile.

    We give Groq the same fingerprint, the Dockerfile it previously produced,
    and the concrete build error. It must return the same JSON schema with an
    UPDATED (fixed) `dockerfile_content`. `attempt` is just for context; the
    loop bounds retries in code, not in the prompt.
    """
    return f"""\
A `docker build` of the previously generated Dockerfile FAILED. Your job is to
patch it and return a corrected Dockerfile, still by filling the SAME template
that is already in use.

## STRICT OUTPUT RULES
- Reply with ONLY a single JSON object. No preamble, no markdown fences, no prose.
- Same JSON schema as before:
{_RESPONSE_SCHEMA}

## REPO FINGERPRINT (JSON) — unchanged from before
{_fingerprint_payload(fingerprint)}

## PREVIOUSLY GENERATED DOCKERFILE (this is what failed)
<dockerfile>
{previous_dockerfile}
</dockerfile>

## DOCKER BUILD ERROR (the reason it failed)
<build_error>
{build_error}
</build_error>

## AVAILABLE TEMPLATES (fill one — prefer reusing the current template)
{template_skeletons}

## GUIDANCE FOR THE PATCH
- Identify the root cause from the build error: missing dependency, wrong base
  image, wrong install command, missing file in COPY, CMD/exec form, port
  mismatch, etc.
- Keep the fix MINIMAL and targeted. Change only what the error demands.
- Do not rewrite working parts. Preserve the framework selection.
- If the error indicates a fundamental mismatch (e.g. the repo is actually
  Python static content), you may switch templates — clearly note it.
- Ensure `dockerfile_content` is the COMPLETE corrected Dockerfile.

This is patch attempt #{attempt}. Return ONLY the JSON object now.
"""
