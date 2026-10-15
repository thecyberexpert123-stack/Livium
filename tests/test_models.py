"""Model layer tests.

This environment has no reachable provider API (egress to api.openai.com,
api.anthropic.com, generativelanguage.googleapis.com and openrouter.ai is
blocked), so these tests do two honest things:

* assert the exact wire format each client produces, which is the part that can be
  verified offline;
* assert that an unreachable provider produces a *reported failure*, never a
  fabricated completion and never a silent fallback.

The scripted client is a test double; where it appears, the test says so.
"""

from __future__ import annotations

import json
import unittest

from support import LiviumTestCase

from livium.models import (
    AnthropicClient,
    Conversation,
    CredentialMissing,
    ModelClient,
    ModelError,
    OllamaClient,
    OpenAICompatibleClient,
    ProviderRejected,
    ProviderUnavailable,
    ScriptedClient,
    ToolCall,
    build_client,
    detect_provider,
    estimate_tokens,
)


class ToolSchemaTests(LiviumTestCase):
    def test_canonical_schemas(self) -> None:
        conversation = Conversation(system="sys")
        conversation.add_user("hello")
        conversation.add_assistant("calling", [ToolCall(id="c1", name="read_file",
                                                       arguments={"path": "a.txt"})])
        conversation.add_tool_result(ToolCall(id="c1", name="read_file", arguments={}), "contents")

        openai_messages = conversation.for_openai()
        self.assertEqual(openai_messages[0], {"role": "system", "content": "sys"})
        self.assertEqual(openai_messages[1]["role"], "user")
        assistant = openai_messages[2]
        self.assertEqual(assistant["tool_calls"][0]["type"], "function")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
                         {"path": "a.txt"})
        self.assertEqual(openai_messages[3]["role"], "tool")
        self.assertEqual(openai_messages[3]["tool_call_id"], "c1")

        anthropic_messages = conversation.for_anthropic()
        blocks = anthropic_messages[1]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "calling"})
        self.assertEqual(blocks[1]["type"], "tool_use")
        self.assertEqual(blocks[1]["input"], {"path": "a.txt"})
        tool_result = anthropic_messages[2]
        self.assertEqual(tool_result["role"], "user")  # Anthropic requires this
        self.assertEqual(tool_result["content"][0]["type"], "tool_result")
        self.assertEqual(tool_result["content"][0]["tool_use_id"], "c1")

        ollama_messages = conversation.for_ollama()
        self.assertEqual(ollama_messages[1]["tool_calls"][0]["function"]["name"], "read_file")


class ClientConstructionTests(LiviumTestCase):
    def test_provider_detection_from_base_url(self) -> None:
        self.assertEqual(detect_provider("https://api.anthropic.com/v1"), "anthropic")
        self.assertEqual(detect_provider("http://127.0.0.1:11434"), "ollama")
        self.assertEqual(detect_provider("https://openrouter.ai/api/v1"), "openai_compatible")

    def test_build_client_selects_the_configured_backend(self) -> None:
        self.assertIsInstance(build_client(self.config(LIVIUM_PROVIDER="openai_compatible")),
                              OpenAICompatibleClient)
        self.assertIsInstance(build_client(self.config(LIVIUM_PROVIDER="anthropic")), AnthropicClient)
        self.assertIsInstance(build_client(self.config(LIVIUM_PROVIDER="ollama")), OllamaClient)

    def test_unknown_provider_is_rejected_at_config_load(self) -> None:
        from livium.config import ConfigError

        with self.assertRaises(ConfigError):
            self.config(LIVIUM_PROVIDER="not-a-provider")

    def test_scripted_client_refuses_to_run_without_the_explicit_test_flag(self) -> None:
        config = self.config(LIVIUM_PROVIDER="scripted", LIVIUM_ALLOW_TEST_DOUBLE="0")
        with self.assertRaises(CredentialMissing):
            ScriptedClient(config)

    def test_scripted_replies_are_permanently_marked_as_test_doubles(self) -> None:
        config = self.config(LIVIUM_PROVIDER="scripted", LIVIUM_ALLOW_TEST_DOUBLE="1")
        client = ScriptedClient(config, [{"text": "hi", "stop_reason": "stop"}])
        reply = client.complete(Conversation(), [])
        self.assertTrue(reply.is_test_double)
        self.assertIn("NO REAL MODEL", client.probe().detail)


class ContextWindowTests(LiviumTestCase):
    def test_known_models_report_their_window(self) -> None:
        cases = {
            "claude-3-5-haiku-latest": 200_000,
            "gpt-4o-mini": 128_000,
            "llama3.1:8b": 8_192,
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(ModelClient._known_context_window(model), expected)

    def test_operator_declaration_wins(self) -> None:
        config = self.config(LIVIUM_CONTEXT_WINDOW="4096")
        client = OpenAICompatibleClient(config)
        window, source = client.context_window()
        self.assertEqual(window, 4096)
        self.assertIn("operator-declared", source)

    def test_unknown_model_falls_back_conservatively_and_says_so(self) -> None:
        config = self.config(LIVIUM_MODEL="mystery-model-9", LIVIUM_FALLBACK_CONTEXT_WINDOW="4096")
        client = OpenAICompatibleClient(config)
        window, source = client.context_window()
        self.assertEqual(window, 4096)
        self.assertIn("conservative fallback", source)

    def test_token_estimator_errs_high(self) -> None:
        text = "a" * 350
        self.assertGreaterEqual(estimate_tokens(text), 100)
        self.assertEqual(estimate_tokens(""), 0)


class UnreachableProviderTests(LiviumTestCase):
    """The provider APIs are blocked here, which makes this the honest test of
    failure behaviour: a real HTTP attempt that fails must be reported."""

    def test_unreachable_provider_raises_provider_unavailable(self) -> None:
        config = self.config(
            LIVIUM_PROVIDER="openai_compatible",
            LIVIUM_BASE_URL="http://127.0.0.1:9",
            LIVIUM_MODEL="gpt-4o-mini",
            LIVIUM_API_KEY="test-key-not-used",
            LIVIUM_MODEL_TIMEOUT="3",
        )
        client = OpenAICompatibleClient(config)
        result = client.probe()
        self.assertFalse(result.ok)
        self.assertIn("unreachable", result.detail)
        with self.assertRaises((ProviderUnavailable, ProviderRejected)):
            client.complete(Conversation(system="s"), [])

    def test_anthropic_without_a_credential_says_exactly_that(self) -> None:
        config = self.config(LIVIUM_PROVIDER="anthropic", LIVIUM_API_KEY="")
        client = AnthropicClient(config)
        result = client.probe()
        self.assertFalse(result.ok)
        self.assertIn("credential", result.detail)
        with self.assertRaises(CredentialMissing):
            client.complete(Conversation(system="s"), [])

    def test_validate_names_the_model_when_the_provider_offers_different_ones(self) -> None:
        config = self.config(LIVIUM_MODEL="does-not-exist")
        client = ScriptedClient(config)
        client.probe = lambda: _FakeProbe(["real-model-a", "real-model-b"])  # type: ignore[method-assign]
        with self.assertRaises(ModelError) as caught:
            client.validate()
        self.assertIn("does-not-exist", str(caught.exception))
        self.assertIn("real-model-a", str(caught.exception))


def _FakeProbe(models):
    from livium.models import ProbeResult

    return ProbeResult(ok=True, detail="fake probe", models=models, model_present=False)


class ArgumentParsingTests(LiviumTestCase):
    def test_double_encoded_arguments_are_unwrapped(self) -> None:
        from livium.models import _parse_arguments

        self.assertEqual(_parse_arguments('{"path": "a"}'), {"path": "a"})
        self.assertEqual(_parse_arguments('"{\\"path\\": \\"a\\"}"'), {"path": "a"})
        self.assertEqual(_parse_arguments({"path": "a"}), {"path": "a"})

    def test_unparseable_arguments_are_surfaced_not_guessed(self) -> None:
        from livium.models import _parse_arguments

        self.assertIn("_raw", _parse_arguments("not json at all"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
