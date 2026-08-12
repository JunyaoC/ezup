"""Stateless model calls, facade over a resolved provider.

Every call is a fresh session: no ``--continue``, no ``--resume``, no shared
state. The prompt goes in, the answer comes back, and nothing survives the
call. Output streams to the console as it arrives.

This module stays the pipeline's single entry point -- ``run``, ``Reply``,
``MECHANICAL``/``SYNTHESIS``, ``extract_json``, ``LLMError`` -- so
``pipeline.py`` never learns which backend answered. ``run`` maps the pipeline's
(model, effort) request to a tier and dispatches to the provider chosen by
:func:`ezchangelog.llm_providers.resolve_provider`. With no environment set that
provider is the historical `claude -p` path, so nothing changes for existing
users; setting ``EZUP_LLM_BASE_URL`` + a key switches the whole pipeline to an
OpenAI-compatible HTTP endpoint.

Two tiers, per the pipeline's split between mechanical and judgment work:

    MECHANICAL  sonnet, low effort   -- reshaping data that is already correct
    SYNTHESIS   opus, max effort     -- deciding what the week actually means
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

MECHANICAL = ("sonnet", "low")
SYNTHESIS = ("opus", "max")

# Reverse of the tier -> (model, effort) map. The pipeline still requests work
# by (model, effort); the provider seam speaks tiers. Resolving here keeps the
# CLI vocabulary (opus/max) from ever reaching an HTTP backend.
_TIER_FOR = {MECHANICAL: "mechanical", SYNTHESIS: "synthesis"}


class LLMError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0

    @property
    def cost(self) -> str:
        return f"${self.cost_usd:.4f}"


def available() -> bool:
    """Whether the resolved provider is usable (CLI on PATH, or HTTP configured)."""
    # Imported lazily so importing llm.py never triggers llm_providers at module
    # load time -- llm_providers imports Reply/LLMError back from here, and the
    # lazy call breaks what would otherwise be an import cycle.
    from ezchangelog.llm_providers import resolve_provider

    return resolve_provider().available()


def run(
    prompt: str,
    *,
    model: str,
    effort: str,
    system: str | None = None,
    on_text: Callable[[str], None] | None = None,
    cwd: str | None = None,
) -> Reply:
    """Run one stateless prompt. Streams deltas to ``on_text`` as they arrive.

    Signature is unchanged so ``pipeline.py`` does not move. The (model, effort)
    pair is translated to a tier here and handed to the resolved provider; the
    provider owns the mapping back to a concrete backend request.
    """
    from ezchangelog.llm_providers import resolve_provider

    # Unknown (model, effort) pairs should never reach here -- the pipeline only
    # ever passes MECHANICAL or SYNTHESIS -- but default to the cheap tier rather
    # than crash, so a stray call degrades instead of failing the journal.
    tier = _TIER_FOR.get((model, effort), "mechanical")
    return resolve_provider().run(
        prompt,
        tier=tier,
        system=system,
        on_text=on_text,
        cwd=cwd,
    )


def extract_json(text: str) -> object:
    """Pull a JSON document out of a reply that may be fenced or padded."""
    body = text.strip()

    if "```" in body:
        for block in body.split("```")[1::2]:
            candidate = block
            # Drop a leading language tag ("json", "jsonc") -- check only the
            # first line, never the first 20 characters of the payload.
            first, newline, rest = block.partition("\n")
            if newline and first.strip().lower() in ("json", "jsonc", ""):
                candidate = rest
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                continue

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass

    # Scan from whichever bracket opens FIRST. Preferring '[' would reach past
    # an object's opening brace and return an inner array instead of the
    # document -- a silent, shape-changing failure.
    candidates = [(body.find(o), o, c) for o, c in (("{", "}"), ("[", "]"))]
    for start, opener, closer in sorted(c for c in candidates if c[0] != -1):
        end = body.rfind(closer)
        if end > start:
            try:
                return json.loads(body[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"no JSON found in reply (first 200 chars): {body[:200]}")
