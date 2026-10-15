// Settings, and the one rule that shapes all of them: the key goes one way.
//
// The operator types it into the browser, it is written 0600, and it reaches the
// runtime through the child process environment. It never comes back out: not in a
// state payload, not in a log line, not in an error message.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { KEY_VARIABLE, childEnvironment, fingerprintOf, publicSettings, readSettings,
         scrub, writeSettings } from "../lib/settings.mjs";

const KEY = "sk-live-abcdefghijklmnopqrstuvwxyz012345";

async function tempHome() {
  return await fs.mkdtemp(path.join(os.tmpdir(), "livium-settings-"));
}

test("a missing settings file is an empty configuration, not an error", async () => {
  const home = await tempHome();
  const settings = await readSettings(home);
  assert.equal(settings.api_key, "");
  assert.equal(settings.provider, "openai_compatible");
  assert.deepEqual(settings.extra, {});
});

test("the settings file is 0600 inside a 0700 directory", { skip: process.platform === "win32" }, async () => {
  const home = await tempHome();
  await writeSettings(home, { api_key: KEY, provider: "openai_compatible" });
  const file = path.join(home, "settings.json");
  const fileMode = (await fs.stat(file)).mode & 0o777;
  const dirMode = (await fs.stat(home)).mode & 0o777;
  assert.equal(fileMode, 0o600, `settings.json is ${fileMode.toString(8)}`);
  assert.equal(dirMode, 0o700, `the home is ${dirMode.toString(8)}`);
});

test("a patch with no key keeps the stored one; clear_api_key removes it", async () => {
  const home = await tempHome();
  await writeSettings(home, { api_key: KEY });
  const kept = await writeSettings(home, { model: "gpt-4o-mini" });
  assert.equal(kept.api_key, KEY);
  assert.equal(kept.model, "gpt-4o-mini");
  const cleared = await writeSettings(home, { clear_api_key: true });
  assert.equal(cleared.api_key, "");
  const reread = await readSettings(home);
  assert.equal(reread.api_key, "");
  assert.equal(reread.model, "gpt-4o-mini");
});

test("extra environment must look like environment variables", async () => {
  const home = await tempHome();
  await writeSettings(home, { extra: { LIVIUM_MAX_STEPS: "12" } });
  assert.deepEqual((await readSettings(home)).extra, { LIVIUM_MAX_STEPS: "12" });
  await assert.rejects(() => writeSettings(home, { extra: { "not a name": "1" } }));
  await assert.rejects(() => writeSettings(home, { extra: "LIVIUM_MAX_STEPS=12" }));
  const emptied = await writeSettings(home, { extra: { LIVIUM_MAX_STEPS: "" } });
  assert.deepEqual(emptied.extra, {});
});

test("what the UI may see never contains the key", async () => {
  const home = await tempHome();
  const settings = await writeSettings(home, { api_key: KEY, provider: "anthropic",
                                                model: "claude-3-5-sonnet-latest" });
  const shown = publicSettings(settings);
  const asText = JSON.stringify(shown);
  assert.equal(asText.includes(KEY), false, "the key is in the settings payload");
  assert.equal(asText.includes(KEY.slice(0, 12)), false, "a prefix of the key is in the payload");
  assert.equal(shown.api_key_set, true);
  assert.equal(shown.api_key_length, KEY.length);
  assert.equal(shown.api_key_hint, `${KEY.slice(0, 3)}…${KEY.slice(-4)} (${KEY.length} characters)`);
  assert.equal("api_key" in shown, false);
});

test("extra environment values are write-only even when the variable name is custom", async () => {
  const home = await tempHome();
  const secret = "custom-value-no-key-shape-at-all";
  const settings = await writeSettings(home, { extra: {
    PRIVATE_DEPLOYMENT_CREDENTIAL: secret, LIVIUM_MAX_STEPS: "9",
  } });
  const shown = publicSettings(settings);
  assert.deepEqual(shown.extra, { PRIVATE_DEPLOYMENT_CREDENTIAL: null, LIVIUM_MAX_STEPS: null });
  assert.equal(JSON.stringify(shown).includes(secret), false);
  assert.equal(scrub(`debug: ${secret}`, settings).includes(secret), false);
  const kept = await writeSettings(home, { provider: "ollama", extra: {} });
  assert.equal(kept.extra.PRIVATE_DEPLOYMENT_CREDENTIAL, secret,
               "saving another field must not replace a secret with a redaction marker");
  const cleared = await writeSettings(home, { extra: { PRIVATE_DEPLOYMENT_CREDENTIAL: "" } });
  assert.equal("PRIVATE_DEPLOYMENT_CREDENTIAL" in cleared.extra, false);
});

test("an unset key is reported as unset rather than blank", async () => {
  const home = await tempHome();
  const shown = publicSettings(await readSettings(home));
  assert.equal(shown.api_key_set, false);
  assert.equal(shown.api_key_length, 0);
  assert.equal(shown.api_key_hint, null);
});

test("the child environment carries the key and nothing empty", async () => {
  const home = await tempHome();
  const settings = await writeSettings(home, { api_key: KEY, provider: "ollama",
                                                model: "llama3.1",
                                                extra: { LIVIUM_MAX_STEPS: "9" } });
  const env = childEnvironment(settings, { PATH: "/usr/bin", LIVIUM_MODEL: "stale-model" });
  assert.equal(env[KEY_VARIABLE], KEY);
  assert.equal(env.LIVIUM_PROVIDER, "ollama");
  assert.equal(env.LIVIUM_MODEL, "llama3.1");
  assert.equal(env.LIVIUM_MAX_STEPS, "9");
  assert.equal(env.PATH, "/usr/bin");
  assert.equal("LIVIUM_BASE_URL" in env, false, "an empty base URL must not shadow the provider default");

  const cleared = await writeSettings(home, { clear_api_key: true, model: "" });
  const clearedEnv = childEnvironment(cleared, { PATH: "/usr/bin" });
  assert.equal(KEY_VARIABLE in clearedEnv, false);
  assert.equal("LIVIUM_MODEL" in clearedEnv, false);
});

test("the test double is announced as a test double", async () => {
  const home = await tempHome();
  const settings = await writeSettings(home, { provider: "scripted",
                                               extra: { LIVIUM_ALLOW_TEST_DOUBLE: "1" } });
  const shown = publicSettings(settings);
  assert.equal(shown.warnings.length, 2);
  assert.match(shown.warnings.join(" "), /test double/);
  assert.match(shown.warnings.join(" "), /no real model/);
});

test("scrub removes the key from anything on its way out", () => {
  assert.equal(scrub(`the provider rejected ${KEY}`, KEY).includes(KEY), false);
  assert.match(scrub(`the provider rejected ${KEY}`, KEY), /redacted/);
  assert.match(scrub("Authorization: Bearer sk-abcdefghijklmnopqrstuvwx", ""), /redacted/);
  assert.match(scrub("key-0123456789abcdefghij", ""), /redacted/);
  assert.equal(scrub("nothing secret here", KEY), "nothing secret here");
  const custom = "a-custom-secret-in-an-unremarkable-variable";
  assert.equal(scrub(`the value was ${custom}`, {
    api_key: KEY, extra: { CUSTOM_OPTION: custom, LIVIUM_MAX_STEPS: "12" },
  }).includes(custom), false, "extra values must be write-only even with ordinary names");
  assert.equal(scrub("12 steps", { api_key: "", extra: { LIVIUM_MAX_STEPS: "12" } }),
               "12 steps", "short non-secret values should not corrupt normal responses");
  assert.equal(scrub(null, KEY), "");
});

test("the fingerprint identifies a key without being one", async () => {
  const home = await tempHome();
  assert.equal(await fingerprintOf({ api_key: "" }), null);
  const first = await fingerprintOf({ api_key: KEY });
  const again = await fingerprintOf({ api_key: KEY });
  const other = await fingerprintOf({ api_key: `${KEY}x` });
  assert.equal(first, again);
  assert.notEqual(first, other);
  assert.equal(first.length, 12);
  assert.equal(KEY.includes(first), false);
  const settings = await readSettings(home);
  assert.equal(settings.api_key, "");
});
