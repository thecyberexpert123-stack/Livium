"""Shared test helpers.

Every test runs against a throwaway home directory, so no test can read or
mutate the developer's real Livium database, workspace or capability lab.

The scripted model client used by orchestration tests is a *test double*. Tests
that use it say so in their docstring, and it is never used to make a claim about
model quality or about a real provider integration. Tests of the real provider
integrations assert on the wire format and on honest failure when the provider is
unreachable, because this environment has no reachable provider API.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TempHome:
    """Isolated LIVIUM_HOME plus a config built from it."""

    def __init__(self, **env: str):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="livium-test-"))
        self.env_overrides = env

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "LIVIUM_HOME": str(self.dir),
            "LIVIUM_DB": str(self.dir / "livium.db"),
            "LIVIUM_WORKSPACE": str(self.dir / "workspace"),
            "LIVIUM_EVIDENCE": str(self.dir / "evidence"),
            # Promotion writes a capability where the loader will import it. Inside
            # tests that must be the temporary home, never the working checkout.
            "LIVIUM_PROMOTED_DIR": str(self.dir / "promoted"),
            "LIVIUM_ALLOW_TEST_DOUBLE": "1",
            "LIVIUM_PROVIDER": "scripted",
        }
        env.update(self.env_overrides)
        env.update(extra)
        return env

    def config(self, **extra: str):
        from livium.config import Config

        return Config.load(env=self.env(**extra))

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class LiviumTestCase(unittest.TestCase):
    """Base class wiring an isolated home and cleanup."""

    def setUp(self) -> None:
        self.home = TempHome()
        self.addCleanup(self.home.cleanup)

    def config(self, **env: str):
        return self.home.config(**env)

    def agent(self, script=None, **env: str):
        from livium.agent import LiviumAgent

        config = self.config(**env)
        agent = LiviumAgent(config, script=script)
        self.addCleanup(agent.close)
        return agent

    def store(self, config=None):
        from livium.store import Store

        config = config or self.config()
        store = Store(config.paths.db)
        self.addCleanup(store.close)
        return store


def with_protected(config, *paths: pathlib.Path):
    """Return a copy of ``config`` whose protected paths are exactly ``paths``.

    Lets a test exercise tamper detection against files it controls, without ever
    writing to the repository's real protected files.
    """

    perms = dataclasses.replace(config.perms, protected_paths=tuple(paths))
    return dataclasses.replace(config, perms=perms)


def env_guard() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith("LIVIUM_")}
