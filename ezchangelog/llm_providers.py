"""Pluggable LLM backends behind the :mod:`ezchangelog.llm` facade.

The pipeline only ever asks for one of two *tiers* -- ``"mechanical"`` (reshape
data that is already correct) or ``"synthesis"`` (decide what the week means).
It never picks a concrete model. That indirection is the whole point of this
module: a provider receives a tier, and each provider is free to translate that
tier into whatever vocabulary its backend speaks.

Two providers ship today:

    ClaudeCliProvider   shells out to `claude -p` (the historical behaviour;
                        the default, so nothing changes for existing users).
    HttpProvider        any OpenAI *chat-completions* compatible endpoint,
                        spoken over stdlib ``urllib`` only.

Why tiers instead of model/effort names on the wire: the Claude-CLI names
(``opus``/``max``) are meaningless to MiniMax or a self-hosted vLLM box, and
handing them raw to an HTTP endpoint would either 400 or, worse, be silently
ignored. The tier is the stable contract; the model id lives in configuration
(``EZUP_LLM_MODEL_MECHANICAL`` / ``EZUP_LLM_MODEL_SYNTHESIS``) where the PM who
chose the provider also chooses the models.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Callable, Mapping, Protocol

# Reply/LLMError/tier tuples live in llm.py (the facade the pipeline imports).
# Importing them here is safe because llm.py defers its own import of this
# module to call time -- there is no import cycle at module load.
from ezchangelog.llm import LLMError, MECHANICAL, SYNTHESIS, Reply

# The two tiers the pipeline speaks. Kept as a tuple so callers and tests can
# validate a tier string without hard-coding the literals twice.
TIERS = ("mechanical", "synthesis")

OnText = Callable[[str], None]


class Provider(Protocol):
    """A backend that can answer one stateless prompt for a given tier.

    ``cwd`` is carried through (not in the design's abbreviated signature)
    because the CLI provider must run `claude` inside the store's agent
    directory -- the GIT corroboration stage resolves repos relative to it.
    HTTP backends have no filesystem context and ignore it.
    """

    def run(
        self,
        prompt: str,
        *,
        tier: str,
        system: str | None = None,
        on_text: OnText | None = None,
        cwd: str | None = None,
    ) -> Reply: ...

    def available(self) -> bool: ...


class ClaudeCliProvider:
    """Wraps the historical `claude -p` streaming behaviour, verbatim.

    This is the default provider, so an existing user who sets no environment
    variables sees byte-for-byte the same behaviour as before the seam existed.
    """

    # Tier -> the (model, effort) pair the CLI understands. These are the same
    # tuples the pipeline used to unpack directly, now owned by the one provider
    # that actually speaks CLI vocabulary.
    _TIER_ARGS = {"mechanical": MECHANICAL, "synthesis": SYNTHESIS}

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def run(
        self,
        prompt: str,
        *,
        tier: str,
        system: str | None = None,
        on_text: OnText | None = None,
        cwd: str | None = None,
    ) -> Reply:
        if not self.available():
            raise LLMError("the `claude` CLI is not on PATH")

        try:
            model, effort = self._TIER_ARGS[tier]
        except KeyError:
            raise LLMError(f"unknown tier {tier!r}")

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


class HttpProvider:
    """An OpenAI chat-completions compatible client on stdlib ``urllib``.

    Speaks the dialect that MiniMax, OpenRouter, vLLM, Ollama, and Anthropic's
    own compatibility endpoint all accept: ``POST {base_url}/chat/completions``
    with ``Authorization: Bearer <api_key>`` and ``"stream": true``, parsing the
    Server-Sent-Events ``data: {...}`` lines and feeding each ``delta.content``
    piece to ``on_text``.

    Only ``urllib`` is used -- no ``requests``/``httpx`` -- because the runner
    image's one permitted third-party dependency is ``cryptography``. urllib's
    incremental ``readline`` is enough to stream an SSE body.

    Pointing at MiniMax (international endpoint), for example::

        EZUP_LLM_BASE_URL=https://api.minimax.io/v1
        EZUP_LLM_MODEL_MECHANICAL=MiniMax-Text-01
        EZUP_LLM_MODEL_SYNTHESIS=MiniMax-M1

    (MiniMax's mainland endpoint is https://api.minimaxi.chat/v1; confirm the
    current model ids in MiniMax's docs -- they version them.) Any other
    OpenAI-compatible base_url and its model ids slot in the same way, which is
    how a PM points this at a self-hosted box the LLM provider never sees.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_map: Mapping[str, str],
        *,
        timeout: float = 600.0,
    ) -> None:
        # Trailing slash trimmed so "{base}/chat/completions" is well-formed
        # whether the PM wrote ".../v1" or ".../v1/".
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_map = dict(model_map)
        self.timeout = timeout

    def available(self) -> bool:
        # No network preflight: the first real call is the honest check, and a
        # HEAD/ping would only add a second failure mode. A configured base_url
        # and key is all "available" can honestly mean here.
        return bool(self.base_url and self.api_key)

    def run(
        self,
        prompt: str,
        *,
        tier: str,
        system: str | None = None,
        on_text: OnText | None = None,
        cwd: str | None = None,  # noqa: ARG002 -- HTTP backends have no cwd
    ) -> Reply:
        model = self.model_map.get(tier)
        if not model:
            raise LLMError(f"no model configured for tier {tier!r}")

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = json.dumps(
            {
                "model": model,
                "messages": messages,
                "stream": True,
                # Ask compatible servers to emit a final usage-bearing chunk;
                # servers that do not understand it ignore the field.
                "stream_options": {"include_usage": True},
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )

        chunks: list[str] = []
        reply = Reply(text="")
        started = time.monotonic()

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        # Blank separators and ":" comment/keepalive lines.
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        # A partial line can arrive if the server chunks oddly;
                        # skip it rather than abort the whole stream.
                        continue
                    # An OpenAI-compatible server can return HTTP 200 and then
                    # an in-band error object (MiniMax does this). Surface it as
                    # the real error rather than letting the stream end empty
                    # and reporting a misleading "empty response". (finding 4)
                    err = event.get("error")
                    if isinstance(err, dict):
                        msg = err.get("message") or json.dumps(err)[:400]
                        raise LLMError(f"provider error: {msg}")
                    self._consume(event, chunks, reply, on_text)
        except urllib.error.HTTPError as error:
            # Non-2xx: surface the body's first 400 chars, mirroring the CLI
            # provider's error hygiene so failures are diagnosable.
            detail = error.read().decode("utf-8", "replace").strip()[:400]
            raise LLMError(f"http {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise LLMError(f"http request failed: {error.reason}") from error
        except (TimeoutError, OSError) as error:
            # A stall or dropped connection *mid-stream* raises here, not as a
            # URLError -- the socket is already iterating. The facade promises
            # LLMError on any failure, so a raw OSError must not escape.
            raise LLMError(f"http stream interrupted: {error}") from error

        reply.duration_ms = int((time.monotonic() - started) * 1000)
        reply.text = "".join(chunks)
        if not reply.text.strip():
            raise LLMError("provider returned an empty response")
        return reply

    @staticmethod
    def _consume(
        event: dict,
        chunks: list[str],
        reply: Reply,
        on_text: OnText | None,
    ) -> None:
        """Fold one SSE JSON event into the running reply."""
        # Usage may ride the final chunk (choices empty) or a mid-stream chunk;
        # take it whenever present. cost_usd stays 0.0 -- these endpoints price
        # out of band, and the console renders a zero cost gracefully.
        usage = event.get("usage")
        if isinstance(usage, dict):
            reply.input_tokens = int(usage.get("prompt_tokens") or 0)
            reply.output_tokens = int(usage.get("completion_tokens") or 0)

        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                chunks.append(piece)
                if on_text:
                    on_text(piece)


def resolve_provider(env: Mapping[str, str] = os.environ) -> Provider:
    """Choose a provider from configuration, read exactly once.

    An HTTP endpoint is used when both a base_url and a key are configured;
    otherwise the CLI provider is returned, so the default path is unchanged for
    anyone who set nothing.
    """
    base_url = (env.get("EZUP_LLM_BASE_URL") or "").strip()
    api_key = _read_key(env)

    if base_url and api_key:
        model_map = {
            "mechanical": (env.get("EZUP_LLM_MODEL_MECHANICAL") or "").strip(),
            "synthesis": (env.get("EZUP_LLM_MODEL_SYNTHESIS") or "").strip(),
        }
        return HttpProvider(base_url, api_key, model_map)

    return ClaudeCliProvider()


def _read_key(env: Mapping[str, str]) -> str:
    """Resolve the bearer key: a ``*_FILE`` path wins over an inline value.

    ``EZUP_LLM_KEY`` is the documented variable. ``EZUP_LLM_API_KEY`` and
    ``EZUP_LLM_API_KEY_FILE`` are honoured too so the runner container contract
    (section 3 of the design, which mounts the key as a file secret) works
    without a second spelling of the same idea.
    """
    key_file = (env.get("EZUP_LLM_API_KEY_FILE") or "").strip()
    if key_file:
        try:
            return open(key_file, encoding="utf-8").read().strip()
        except OSError as error:
            raise LLMError(f"cannot read EZUP_LLM_API_KEY_FILE: {error}") from error

    for name in ("EZUP_LLM_KEY", "EZUP_LLM_API_KEY"):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""
