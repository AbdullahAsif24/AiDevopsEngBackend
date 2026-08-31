"""Thin, async-friendly wrapper around the Groq SDK.

We use the official `groq` SDK directly (no LangChain). The SDK is sync-only,
so we run `.create()` in a ThreadPoolExecutor to avoid blocking the event loop —
which matters because many jobs can be generating Dockerfiles concurrently.

All network/validation errors are surfaced as GroqCallError; the caller decides
whether to retry.
"""
from __future__ import annotations

import asyncio

from groq import AsyncGroq

from ..config import settings


class GroqCallError(Exception):
    """Raised when a Groq call fails (network, auth, malformed JSON response)."""


async def _query_groq(prompt: str) -> str:
    """Send one prompt to Groq and return the raw text (assumed to be JSON).

    Uses response_format json_object so the model is constrained to emit JSON.
    The model field + temperature come from settings (shared, immutable).
    """
    # Settings are read once at import; calling it here keeps it obvious.
    client = AsyncGroq(api_key=settings.groq_api_key)

    try:
        completion = await client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise backend that always returns strict JSON "
                        "matching the exact schema requested. Never add prose or "
                        "markdown fences around your JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=settings.groq_temperature,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # SDK raises a family of exceptions; wrap generically.
        raise GroqCallError(f"Groq request failed: {exc}") from exc

    content = completion.choices[0].message.content
    if not content:
        raise GroqCallError("Groq returned an empty response")

    return content
