"""Stateless model calls via ``claude -p``.

Every call is a fresh headless session: no ``--continue``, no ``--resume``, no
shared state. The prompt goes in on stdin, the answer comes back on stdout, and
nothing survives the process. Output streams to the console as it arrives.

Two tiers, per the pipeline's split between mechanical and judgment work:

    MECHANICAL  sonnet, low effort   -- reshaping data that is already correct
    SYNTHESIS   opus, max effort     -- deciding what the week actually means
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

MECHANICAL = ("sonnet", "low")
SYNTHESIS = ("opus", "max")


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
    return shutil.which("claude") is not None


def run(
    prompt: str,
    *,
    model: str,
    effort: str,
    system: str | None = None,
    on_text: Callable[[str], None] | None = None,
    cwd: str | None = None,
) -> Reply:
    """Run one stateless prompt. Streams deltas to ``on_text`` as they arrive."""
    if not available():
        raise LLMError("the `claude` CLI is not on PATH")

    command = [
        "claude",
        "-p",
        "--model", model,
        "--effort", effort,
        # No MCP servers: they add tool definitions to every request and this
        # call needs no tools at all.
        "--strict-mcp-config",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if system:
        command += ["--system-prompt", system]

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
    )
    assert process.stdin and process.stdout
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError as error:
        raise LLMError(f"claude closed stdin early: {error}") from error

    chunks: list[str] = []
    reply = Reply(text="")

    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        if kind == "stream_event":
            inner = event.get("event", {})
            if inner.get("type") == "content_block_delta":
                piece = inner.get("delta", {}).get("text", "")
                if piece:
                    chunks.append(piece)
                    if on_text:
                        on_text(piece)
        elif kind == "result":
            usage = event.get("usage") or {}
            reply.cost_usd = float(event.get("total_cost_usd") or 0.0)
            reply.duration_ms = int(event.get("duration_api_ms") or 0)
            reply.input_tokens = int(usage.get("input_tokens") or 0) + int(
                usage.get("cache_read_input_tokens") or 0
            ) + int(usage.get("cache_creation_input_tokens") or 0)
            reply.output_tokens = int(usage.get("output_tokens") or 0)
            if event.get("is_error"):
                raise LLMError(str(event.get("result", "claude reported an error")))
            if not chunks and isinstance(event.get("result"), str):
                chunks.append(event["result"])

    stderr = process.stderr.read() if process.stderr else ""
    code = process.wait()
    if code != 0:
        raise LLMError(f"claude exited {code}: {stderr.strip()[:400]}")

    reply.text = "".join(chunks)
    if not reply.text.strip():
        raise LLMError("claude returned an empty response")
    return reply


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
