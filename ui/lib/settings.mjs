// Studio settings: the provider, the model, and the API key.
//
// The key is the one secret this project handles, so the rules are explicit:
//
//   * it is written to `<studio home>/settings.json`, mode 0600, in a directory
//     mode 0700 -- the same file holds no other secret and is never in the repo;
//   * it is handed to a child process through its environment, never through a
//     command line (a shell command line is visible in `ps` to every user);
//   * every response carries `api_key_set`, a deliberate `api_key_hint` and the
//     length, and never the value -- not even for the request that sets it;
//   * any string the Studio returns is passed through `scrub()` first, so a key
//     that came back inside an error message from somewhere else is redacted too.

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

export const SETTINGS_FILE = "settings.json";
export const KEY_VARIABLE = "LIVIUM_API_KEY";

//: Environment variables the runtime reads. Anything else the operator adds lands in
//: `extra` and is passed through, because a deployment may need settings this UI has
//: never heard of -- but the known ones get a warning when they change what the
//: runtime is actually proving.
export const KNOWN_VARIABLES = Object.freeze([
  "LIVIUM_PROVIDER", "LIVIUM_MODEL", "LIVIUM_BASE_URL", "LIVIUM_API_KEY",
  "LIVIUM_MODEL_TIMEOUT", "LIVIUM_MODEL_RETRIES",
]);

export const HONESTY_WARNINGS = Object.freeze({
  LIVIUM_ALLOW_TEST_DOUBLE:
    "the scripted provider is a test double: it produces no real model output, and " +
    "every record it touches is marked as such. Do not read a result from it as " +
    "evidence about a model.",
  LIVIUM_MAX_TOOL_CALLS: "a low tool-call limit truncates tasks rather than making them faster.",
});

function settingsPath(studioHome) {
  return path.join(studioHome, SETTINGS_FILE);
}

/** Read the settings file. A missing file is an empty configuration, not an error. */
export async function readSettings(studioHome) {
  try {
    const raw = await fs.readFile(settingsPath(studioHome), "utf8");
    const parsed = JSON.parse(raw);
    return {
      provider: String(parsed.provider ?? "openai_compatible"),
      model: String(parsed.model ?? ""),
      base_url: String(parsed.base_url ?? ""),
      api_key: typeof parsed.api_key === "string" ? parsed.api_key : "",
      extra: parsed.extra && typeof parsed.extra === "object" ? { ...parsed.extra } : {},
      updated_at: parsed.updated_at ?? null,
    };
  } catch (error) {
    if (error.code === "ENOENT") {
      return { provider: "openai_compatible", model: "", base_url: "", api_key: "",
               extra: {}, updated_at: null };
    }
    throw new Error(`the settings file is unreadable: ${error.message}`);
  }
}

/** Merge a patch into the settings. A patch with `api_key: null` keeps the old key. */
export async function writeSettings(studioHome, patch) {
  const current = await readSettings(studioHome);
  const next = {
    provider: patch.provider !== undefined ? String(patch.provider).trim() : current.provider,
    model: patch.model !== undefined ? String(patch.model).trim() : current.model,
    base_url: patch.base_url !== undefined ? String(patch.base_url).trim() : current.base_url,
    api_key: current.api_key,
    extra: { ...current.extra },
    updated_at: new Date().toISOString(),
  };
  if (patch.api_key !== undefined && patch.api_key !== null) {
    next.api_key = String(patch.api_key);
  }
  if (patch.clear_api_key === true) next.api_key = "";
  if (patch.extra !== undefined) {
    if (patch.extra === null) {
      next.extra = {};
    } else if (typeof patch.extra !== "object") {
      throw new Error("extra environment must be an object of name -> value");
    } else {
      for (const [name, value] of Object.entries(patch.extra)) {
        if (!/^[A-Z][A-Z0-9_]*$/.test(name)) {
          throw new Error(`${JSON.stringify(name)} is not an environment variable name`);
        }
        if (value === null || value === "") delete next.extra[name];
        else next.extra[name] = String(value);
      }
    }
  }
  await fs.mkdir(studioHome, { recursive: true, mode: 0o700 });
  const target = settingsPath(studioHome);
  const handle = await fs.open(target, "w", 0o600);
  try {
    await handle.writeFile(JSON.stringify(next, null, 2), "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  await fs.chmod(target, 0o600); // open() honours umask; the secret file does not get to.
  return next;
}

/** The environment a child process receives: the operator's, plus the settings. */
export function childEnvironment(settings, base = process.env) {
  const env = { ...base, ...settings.extra };
  env.LIVIUM_PROVIDER = settings.provider;
  if (settings.model) env.LIVIUM_MODEL = settings.model;
  else delete env.LIVIUM_MODEL;
  if (settings.base_url) env.LIVIUM_BASE_URL = settings.base_url;
  else delete env.LIVIUM_BASE_URL;
  if (settings.api_key) env[KEY_VARIABLE] = settings.api_key;
  else delete env[KEY_VARIABLE];
  return env;
}

/** The settings as the UI may see them: everything except the secret. */
export function publicSettings(settings) {
  const key = settings.api_key ?? "";
  const warnings = [];
  for (const name of Object.keys(settings.extra ?? {})) {
    if (HONESTY_WARNINGS[name]) warnings.push(HONESTY_WARNINGS[name]);
  }
  if (settings.provider === "scripted" || settings.extra?.LIVIUM_ALLOW_TEST_DOUBLE === "1") {
    warnings.push("the runtime is configured for the scripted test double, so no real " +
                  "model is involved in anything this home does.");
  }
  return {
    provider: settings.provider,
    model: settings.model,
    base_url: settings.base_url,
    extra: { ...(settings.extra ?? {}) },
    api_key_set: key.length > 0,
    api_key_length: key.length,
    api_key_hint: hint(key),
    updated_at: settings.updated_at,
    warnings,
  };
}

function hint(key) {
  if (!key) return null;
  if (key.length <= 10) return `${"•".repeat(key.length)} (${key.length} characters)`;
  return `${key.slice(0, 3)}…${key.slice(-4)} (${key.length} characters)`;
}

/** Remove any occurrence of the key (and long secrets) from text on its way out. */
export function scrub(text, key) {
  let out = String(text ?? "");
  if (key && key.length >= 8) {
    out = out.split(key).join("«the API key was here and has been redacted»");
  }
  // Provider keys have recognisable shapes; an unknown one that leaks through some
  // error path is still redacted rather than shown in a browser.
  out = out.replace(/\b(sk|pk|api|key)[-_][A-Za-z0-9-_]{16,}\b/g, "«redacted»");
  return out;
}

export async function fingerprintOf(settings) {
  if (!settings.api_key) return null;
  return crypto.createHash("sha256").update(settings.api_key).digest("hex").slice(0, 12);
}
