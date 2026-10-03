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
- **Node/Express/plain-HTTP**: Use the `node` template when package.json exists
  and the entry point is a .js file. This includes plain `http.createServer` apps
  with NO framework — they are still Node containers, NOT static sites.
  - Set `$START_COMMAND` from the package.json `start` script (e.g. `node index.js`)
    or `main` field. Default: `node index.js`.
  - For `$PORT`: scan the entry file for `.listen(PORT)` or
    `process.env.PORT || <number>` / `process.env.PORT ?? <number>`.
    Default to **3000** for Node if no explicit port is found.
- **Python**: Use the `python` template when requirements.txt or pyproject.toml
  exists and the entry is a .py file. Default port 8000.
- **Static**: Use the `static` template ONLY for repos that have NO server entry
  point at all — pure HTML/CSS/JS in a dist/ or public/ folder, or a build output.
  Do NOT classify a Node.js server repo as static.
- Java / compiled languages are OUT OF SCOPE. If the repo doesn't clearly map to
  one of the three templates, choose the closest and note it in metadata.
- Pick the correct template, then substitute:
  * $FRAMEWORK_NOTE  -> short human description, e.g. "plain Node.js HTTP", or
                        "Express for Node", or "FastAPI (Python)".
  * $PORT            -> the actual port number (integer, no quotes).
  * $START_COMMAND   -> how to launch, e.g. "node index.js", or
                        "uvicorn main:app --host 0.0.0.0 --port 8000".
  * $STATIC_SOURCE   -> for static only: the directory to copy, usually 'dist' or '.'.
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
