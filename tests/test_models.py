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
from unittest import mock

from support import LiviumTestCase

from livium.models import (
    AnthropicClient,
    Conversation,
    CredentialMissing,
    ModelClient,
    ModelError,
    NvidiaRequestPacer,
    OllamaClient,
    OpenAICompatibleClient,
    ProviderRejected,
    ProviderUnavailable,
    ScriptedClient,
    ToolCall,
    _retry_after_seconds,
    build_client,
    detect_provider,
    estimate_tokens,
    normalize_token_usage,
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


class NvidiaRequestPacingTests(LiviumTestCase):
    def test_requests_share_a_persistent_sub_40_rpm_schedule(self) -> None:
        first = NvidiaRequestPacer(self.config().paths.home)
        second = NvidiaRequestPacer(self.config().paths.home)
        with mock.patch("livium.models.time.time", return_value=100.0):
            with mock.patch("livium.models.time.sleep") as sleep:
                self.assertEqual(first.wait_for_slot(), 0.0)
                self.assertAlmostEqual(second.wait_for_slot(), 1.6)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 1.6)

    def test_pacer_does_not_use_the_key_as_a_storage_identifier(self) -> None:
        config = self.config(LIVIUM_API_KEY="distinct-test-secret")
        pacer = NvidiaRequestPacer(config.paths.home)
        pacer.wait_for_slot()
        database = pacer.path.read_bytes()
        self.assertNotIn(b"distinct-test-secret", database)

    def test_nvidia_openai_compatible_request_sets_nonempty_agent_content(self) -> None:
        config = self.config(
            LIVIUM_PROVIDER="openai_compatible",
            LIVIUM_BASE_URL="https://integrate.api.nvidia.com/v1",
            LIVIUM_MODEL="nvidia/nemotron-3-ultra-550b-a55b",
        )
        client = OpenAICompatibleClient(config)
        response = {"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]}
        with mock.patch.object(client, "_request", return_value=response) as request:
            reply = client.complete(Conversation(system="instructions"), [])
        self.assertEqual(reply.text, "ready")
        body = request.call_args.args[1]
        self.assertEqual(
            body["chat_template_kwargs"],
            {"enable_thinking": True, "force_nonempty_content": True,
             "reasoning_budget": 4096},
        )

    def test_nvidia_http_request_reserves_a_slot_before_sending(self) -> None:
        config = self.config(LIVIUM_BASE_URL="https://integrate.api.nvidia.com/v1")
        client = OpenAICompatibleClient(config)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with mock.patch("livium.models.NvidiaRequestPacer.wait_for_slot") as reserve:
            with mock.patch("livium.models.urllib.request.urlopen", return_value=response) as send:
                self.assertEqual(
                    client._request("https://integrate.api.nvidia.com/v1/models", None, {},
                                    method="GET"),
                    {},
                )
        reserve.assert_called_once_with()
        send.assert_called_once()


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


class TokenUsageTests(LiviumTestCase):
    def test_nested_or_malformed_counts_do_not_escape_as_numeric_usage(self) -> None:
        usage = normalize_token_usage({
            "prompt_tokens": {"cached_tokens": 12},
            "input_tokens": 24,
            "completion_tokens": {"reasoning_tokens": 6},
            "output_tokens": 8,
            "prompt_tokens_details": {"cached_tokens": 12},
        })
        self.assertEqual(usage, {"prompt_tokens": 24, "completion_tokens": 8})

    def test_invalid_numeric_values_are_ignored_without_raising(self) -> None:
        usage = normalize_token_usage({
            "prompt_tokens": float("inf"),
            "input_tokens": "not-a-count",
            "completion_tokens": float("nan"),
        })
        self.assertEqual(usage, {})

    def test_non_object_usage_is_treated_as_unavailable(self) -> None:
        self.assertEqual(normalize_token_usage([{"prompt_tokens": 3}]), {})

    def test_rate_limit_wait_is_read_from_provider_header_or_error_body(self) -> None:
        self.assertEqual(_retry_after_seconds("4.5", "{}"), 4.5)
        body = json.dumps({"error": {"message": "Please try again in 6.2025s."}})
        self.assertAlmostEqual(_retry_after_seconds(None, body), 6.2025)
        self.assertIsNone(_retry_after_seconds(None, '{"error":{"message":"quota exhausted"}}'))


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
