"""Configuration and documentation must not drift apart.

A documented setting that the code ignores is a lie told to an operator, and an
undocumented setting is a surprise. Both directions are checked here, against the
real files, so the two can only change together.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
PACKAGE = REPO_ROOT / "livium"


def documented_variables() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*#?\s*(LIVIUM_[A-Z0-9_]+)=", text, re.MULTILINE))


def read_variables() -> set[str]:
    found: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        found |= set(re.findall(r'"(LIVIUM_[A-Z0-9_]+)"', path.read_text(encoding="utf-8")))
    return found


def active_example_values() -> dict[str, str]:
    """The uncommented assignments in .env.example, as an environment."""

    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


class EnvironmentDocumentationTests(unittest.TestCase):
    def test_every_documented_variable_is_read_by_the_code(self) -> None:
        undocumented_in_code = sorted(documented_variables() - read_variables())
        self.assertEqual(
            undocumented_in_code, [],
            f".env.example documents settings nothing reads: {undocumented_in_code}",
        )

    def test_every_variable_the_code_reads_is_documented(self) -> None:
        undocumented_in_docs = sorted(read_variables() - documented_variables())
        self.assertEqual(
            undocumented_in_docs, [],
            f"the code reads settings .env.example never mentions: {undocumented_in_docs}",
        )

    def test_the_documented_environment_produces_a_working_configuration(self) -> None:
        """The template is usable as written, not merely present."""

        from livium.config import Config

        env = active_example_values()
        env["LIVIUM_HOME"] = "/tmp/livium-config-doc-test"
        self.assertIn("LIVIUM_PROVIDER", env, "the template must set a provider")

        config = Config.load(env=env)
        self.assertEqual(config.model.provider, "openai_compatible")
        self.assertEqual(config.model.model, "gpt-4o-mini")
        self.assertEqual(config.model.max_output_tokens, 1024)
        self.assertEqual(config.model.temperature, 0.0)
        self.assertEqual(config.limits.max_steps, 12)
        self.assertEqual(config.limits.max_tool_calls, 40)
        self.assertEqual(config.log_level, "info")
        # A documented empty API key is not an error at load time; it becomes an
        # honest CredentialMissing when the provider is actually called.
        self.assertEqual(config.model.api_key, "")

    def test_the_template_never_carries_a_real_credential(self) -> None:
        """Credential-shaped settings must ship empty.

        Matched by suffix rather than substring: ``LIVIUM_MAX_OUTPUT_TOKENS`` is a
        budget, not a secret.
        """

        credential_suffixes = ("_API_KEY", "_SECRET", "_PASSWORD", "_CREDENTIAL", "_TOKEN")
        checked = 0
        for key, value in active_example_values().items():
            if key.endswith(credential_suffixes):
                checked += 1
                self.assertEqual(value, "", f"{key} must be empty in the template")
        self.assertEqual(checked, 1, "expected exactly one credential setting in the template")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
