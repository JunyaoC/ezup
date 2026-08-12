"""Provider seam tests: resolution, HTTP streaming, and tier-name isolation.

These pin the contract described in REMOTE-RUNNER-DESIGN.md sections 2-3:

  * ``resolve_provider`` picks the CLI provider when nothing is configured and
    the HTTP provider the moment a base_url + key exist, translating the two
    pipeline tiers into the PM-chosen concrete model ids.
  * ``HttpProvider`` speaks OpenAI chat-completions SSE, streaming each delta to
    ``on_text`` and folding the usage object into the returned ``Reply``.
  * The Claude-CLI vocabulary (``opus``/``max``) must NEVER reach an HTTP
    endpoint on the wire -- only the configured ``EZUP_LLM_MODEL_*`` ids may.

All HTTP is faked at ``urllib.request.urlopen``; no socket is ever opened.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from ezchangelog import llm
from ezchangelog.llm_providers import (
    ClaudeCliProvider,
    HttpProvider,
    resolve_provider,
)


# A canned OpenAI-style streaming body: two content deltas, then a final
# choices-empty chunk that carries usage, then the sentinel. Bytes, because a
# real urllib response iterates raw bytes lines and HttpProvider decodes them.
CANNED_SSE: list[bytes] = [
    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
    b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
    b"\n",  # blank separator line the parser must tolerate
    b": keepalive comment\n",  # SSE comment line the parser must skip
    b'data: {"choices":[{"delta":{"content":", world"}}]}\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":5}}\n',
    b"data: [DONE]\n",
]


class FakeResponse:
    """Stand-in for the object ``urllib.request.urlopen`` returns.

    Supports the two things ``HttpProvider.run`` uses: the ``with`` context
    manager protocol and line iteration yielding ``bytes``.
    """

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __iter__(self):
        return iter(self._lines)


class _Capture:
    """Records the Request passed to a faked ``urlopen`` so a test can inspect
    exactly what would have gone on the wire."""

    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines
        self.request: Any = None

    def urlopen(self, request: Any, timeout: float | None = None) -> FakeResponse:
        self.request = request
        return FakeResponse(self.lines)

    def sent_body(self) -> dict[str, Any]:
        assert self.request is not None, "urlopen was never called"
        return json.loads(self.request.data.decode("utf-8"))


class ResolveProviderTests(unittest.TestCase):
    def test_no_env_yields_the_cli_provider(self) -> None:
        # An empty environment is the default path: nothing configured means the
        # historical `claude -p` provider, so existing users are untouched.
        provider = resolve_provider({})
        self.assertIsInstance(provider, ClaudeCliProvider)

    def test_base_url_and_key_yield_http_with_per_tier_models(self) -> None:
        env = {
            "EZUP_LLM_BASE_URL": "https://api.example.test/v1",
            "EZUP_LLM_KEY": "sk-secret",
            "EZUP_LLM_MODEL_MECHANICAL": "cheap-mech-01",
            "EZUP_LLM_MODEL_SYNTHESIS": "smart-synth-01",
        }
        provider = resolve_provider(env)
        self.assertIsInstance(provider, HttpProvider)
        # Each tier maps to the PM-configured concrete id, never a tier alias.
        self.assertEqual(provider.model_map["mechanical"], "cheap-mech-01")
        self.assertEqual(provider.model_map["synthesis"], "smart-synth-01")
        # The trailing-slash normalization keeps "{base}/chat/completions" valid.
        self.assertEqual(provider.base_url, "https://api.example.test/v1")

    def test_base_url_without_key_stays_on_the_cli_provider(self) -> None:
        # Half a configuration is not a configuration: a base_url with no key
        # must not silently produce an unauthenticated HTTP provider.
        provider = resolve_provider({"EZUP_LLM_BASE_URL": "https://api.example.test/v1"})
        self.assertIsInstance(provider, ClaudeCliProvider)


class HttpProviderStreamingTests(unittest.TestCase):
    def _provider(self) -> HttpProvider:
        return HttpProvider(
            "https://api.example.test/v1",
            "sk-secret",
            {"mechanical": "cheap-mech-01", "synthesis": "smart-synth-01"},
        )

    def test_streams_deltas_and_accounts_tokens_from_usage(self) -> None:
        provider = self._provider()
        capture = _Capture(CANNED_SSE)
        seen: list[str] = []

        with mock.patch(
            "ezchangelog.llm_providers.urllib.request.urlopen", capture.urlopen
        ):
            reply = provider.run(
                "summarize",
                tier="synthesis",
                on_text=seen.append,
            )

        # (a) every content delta reached on_text, in order.
        self.assertEqual(seen, ["Hello", ", world"])
        # (b) the reply text is the concatenation of the deltas...
        self.assertEqual(reply.text, "Hello, world")
        # ...and the token counts come straight from the usage object.
        self.assertEqual(reply.input_tokens, 11)
        self.assertEqual(reply.output_tokens, 5)

    def test_empty_stream_raises_llm_error(self) -> None:
        # A stream that yields no content is a failure, not an empty success:
        # the pipeline must be able to distinguish "the model said nothing" from
        # "the model returned text", mirroring the CLI provider's guard.
        provider = self._provider()
        capture = _Capture([b"data: [DONE]\n"])
        with mock.patch(
            "ezchangelog.llm_providers.urllib.request.urlopen", capture.urlopen
        ):
            with self.assertRaises(llm.LLMError):
                provider.run("summarize", tier="mechanical")


class TierNameIsolationTests(unittest.TestCase):
    """The whole reason for the tier indirection: CLI names never hit the wire."""

    def test_http_provider_sends_the_configured_synthesis_model(self) -> None:
        provider = HttpProvider(
            "https://api.example.test/v1",
            "sk-secret",
            {"mechanical": "cheap-mech-01", "synthesis": "smart-synth-01"},
        )
        capture = _Capture(CANNED_SSE)
        with mock.patch(
            "ezchangelog.llm_providers.urllib.request.urlopen", capture.urlopen
        ):
            provider.run("compose", tier="synthesis")

        body = capture.sent_body()
        self.assertEqual(body["model"], "smart-synth-01")
        # The Claude-CLI aliases must appear nowhere in the outgoing model id.
        self.assertNotIn(body["model"], ("opus", "max", "sonnet", "low"))

    def test_facade_maps_opus_max_to_the_configured_id_not_opus(self) -> None:
        # The real leak path: pipeline.py asks llm.run for ("opus", "max"). That
        # must be translated to the synthesis tier and go out as the configured
        # model id -- the string "opus" must never appear on the wire.
        env = {
            "EZUP_LLM_BASE_URL": "https://api.example.test/v1",
            "EZUP_LLM_KEY": "sk-secret",
            "EZUP_LLM_MODEL_MECHANICAL": "cheap-mech-01",
            "EZUP_LLM_MODEL_SYNTHESIS": "smart-synth-01",
        }
        capture = _Capture(CANNED_SSE)
        with mock.patch.dict("os.environ", env, clear=False), mock.patch(
            "ezchangelog.llm_providers.urllib.request.urlopen", capture.urlopen
        ):
            reply = llm.run("compose", model="opus", effort="max")

        body = capture.sent_body()
        self.assertEqual(body["model"], "smart-synth-01")
        raw = capture.request.data.decode("utf-8")
        self.assertNotIn("opus", raw)
        self.assertNotIn('"max"', raw)
        # The facade still returns a real Reply built from the fake stream.
        self.assertEqual(reply.text, "Hello, world")

    def test_facade_maps_sonnet_low_to_the_mechanical_id(self) -> None:
        env = {
            "EZUP_LLM_BASE_URL": "https://api.example.test/v1",
            "EZUP_LLM_KEY": "sk-secret",
            "EZUP_LLM_MODEL_MECHANICAL": "cheap-mech-01",
            "EZUP_LLM_MODEL_SYNTHESIS": "smart-synth-01",
        }
        capture = _Capture(CANNED_SSE)
        with mock.patch.dict("os.environ", env, clear=False), mock.patch(
            "ezchangelog.llm_providers.urllib.request.urlopen", capture.urlopen
        ):
            llm.run("brief", model="sonnet", effort="low")

        self.assertEqual(capture.sent_body()["model"], "cheap-mech-01")


if __name__ == "__main__":
    unittest.main()
