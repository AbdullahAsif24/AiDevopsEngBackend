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


def build_generation_prompt(
    fingerprint: RepoFingerprint,
    template_skeletons: str,
    recommended_framework: str | None = None,
) -> str:
    """Prompt for the FIRST pass: analyze the repo and produce a Dockerfile."""
    hint = ""
    if recommended_framework:
        hint = (
            f"\n- Detector hint: prefer framework '{recommended_framework}' unless "
            "the fingerprint clearly contradicts it.\n"
        )

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
- Detect the stack carefully:
  * framework=node ONLY if package.json has a real HTTP server (express/fastify/koa)
    or a start script that runs a long-lived server (node server.js).
  * framework=static if vite.config.*, Vite/React SPA, CRA, or plain HTML —
    MUST use the static multi-stage template (npm run build → nginx :80).
    NEVER use `npm start` / `vite` dev server for production Docker.
  * framework=python if requirements.txt / pyproject.toml + .py entry.
{hint}- Pick the correct template, then substitute:
  * $FRAMEWORK_NOTE  -> short human description.
  * $PORT            -> listen port (3000 node, 8000 python, 80 static/nginx).
  * $START_COMMAND   -> launch command for node/python only.
  * $STATIC_SOURCE   -> build output dir: usually 'dist' (Vite) or 'build' (CRA).
- For static: port MUST be 80, dockerfile_content MUST be multi-stage with nginx,
  include ARG/ENV for VITE_* build args, and COPY nginx.spa.conf.
- In the returned JSON: `dockerfile_content` must be the COMPLETE Dockerfile.
- If an existing Dockerfile was provided, prefer adapting it when valid.

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
