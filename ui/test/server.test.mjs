// The Studio's HTTP surface, exercised over a real socket.
//
// The backend is not started here (`LIVIUM_STUDIO_NO_BACKEND`), because these tests are
// about the contract the operator's browser sees: what the console answers, what it
// refuses, and what leaves the process. The one thing asserted throughout is that the
// API key does not: a state payload, an error message and a settings response are all
// checked for it by string.

import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createStudio } from "../server.mjs";
import { findPython } from "../lib/backend.mjs";
import { writeSettings } from "../lib/settings.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

const KEY = "sk-live-abcdefghijklmnopqrstuvwxyz012345";
const WRITE = { "X-Livium-Studio": "1", "Content-Type": "application/json" };
process.env.LIVIUM_STUDIO_NO_BACKEND = "1";

async function interpreter() {
  try {
    return await findPython();
  } catch {
    return null;
  }
}

const python = await interpreter();
const missing = python ? false : "no Python interpreter on PATH; the Studio is a front end to one";

async function withStudio(t, run) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "livium-studio-"));
  await fs.mkdir(path.join(home, "workspace"), { recursive: true });
  const studio = await createStudio({ home, port: 0, host: "127.0.0.1", python });
  const address = await studio.listen();
  const base = `http://127.0.0.1:${address.port}`;
  t.after(async () => { await studio.close(); });
  return await run({ base, home, studio });
}

test("the state the UI polls carries no key material", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    await fetch(`${base}/studio/api/settings`, {
      method: "POST", headers: WRITE,
      body: JSON.stringify({ api_key: KEY, provider: "anthropic", restart: false }),
    });
    const state = await (await fetch(`${base}/studio/api/state`)).json();
    const text = JSON.stringify(state);
    assert.equal(text.includes(KEY), false, "the key is in the state payload");
    assert.equal(text.includes(KEY.slice(0, 10)), false, "a prefix of the key is in the state payload");
    assert.equal(state.settings.api_key_set, true);
    assert.equal(state.settings.api_key_length, KEY.length);
    assert.equal(state.studio.home, await fs.realpath(home));
    assert.equal(state.backend.running, false, "no backend should be spawned in this test");
    assert.equal(state.backend.last_error, null);
  });
});

test("a write without the Studio header is refused", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    const response = await fetch(`${base}/studio/api/settings`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: KEY }),
    });
    assert.equal(response.status, 403);
    const payload = await response.json();
    assert.match(payload.error, /X-Livium-Studio/);
    await assert.rejects(() => fs.stat(path.join(home, "settings.json")));
  });
});

test("saving the key writes it 0600 and never echoes it", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    const response = await fetch(`${base}/studio/api/settings`, {
      method: "POST", headers: WRITE,
      body: JSON.stringify({ api_key: KEY, provider: "openai_compatible",
                             base_url: "https://example.invalid/v1", restart: false }),
    });
    assert.equal(response.status, 200);
    const text = await response.text();
    assert.equal(text.includes(KEY), false, "the settings response echoed the key");
    const payload = JSON.parse(text);
    assert.equal(payload.settings.api_key_set, true);
    assert.match(payload.settings.api_key_hint, /characters\)$/);
    assert.equal(payload.key_fingerprint.length, 12);
    assert.match(payload.note, /previous environment/);

    const stored = JSON.parse(await fs.readFile(path.join(home, "settings.json"), "utf8"));
    assert.equal(stored.api_key, KEY, "the key must be stored, or the runtime cannot use it");
    assert.equal((await fs.stat(path.join(home, "settings.json"))).mode & 0o777, 0o600);
  });
});

test("an upload lands in the workspace and comes back byte-identical", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    const body = Buffer.from("bolts,12\nnuts,30\n", "utf8");
    const digest = crypto.createHash("sha256").update(body).digest("hex");
    const upload = await fetch(`${base}/studio/api/workspace/file?root=workspace`, {
      method: "POST",
      headers: { "X-Livium-Studio": "1", "X-Livium-Path": "brief.txt",
                 "X-Livium-Directory": "uploads", "Content-Type": "application/octet-stream" },
      body,
    });
    assert.equal(upload.status, 201);
    const placed = await upload.json();
    assert.equal(placed.path, path.join("uploads", "brief.txt"));
    assert.equal(placed.sha256, digest);
    assert.equal(await fs.readFile(path.join(home, "workspace", "uploads", "brief.txt"), "utf8"),
                 body.toString("utf8"));

    const preview = await (await fetch(`${base}/studio/api/workspace/file?root=workspace&path=uploads/brief.txt`)).json();
    assert.equal(preview.sha256, digest);
    assert.equal(preview.kind, "text");
    assert.equal(preview.content, body.toString("utf8"));
    assert.equal(preview.truncated, false);

    const download = await fetch(`${base}/studio/api/workspace/file?root=workspace&path=uploads/brief.txt&download=1`);
    assert.equal(download.headers.get("content-disposition").startsWith("attachment;"), true);
    assert.deepEqual(Buffer.from(await download.arrayBuffer()), body);

    const listing = await (await fetch(`${base}/studio/api/workspace?root=workspace&path=uploads`)).json();
    assert.deepEqual(listing.entries.map((entry) => entry.name), ["brief.txt"]);
    assert.equal(listing.entries[0].size, body.length);
  });
});

test("paths that climb out or follow a symlink out are refused", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    const workspace = path.join(home, "workspace");
    const outside = path.join(home, "outside.txt");
    await fs.writeFile(outside, "outside\n");
    await fs.symlink(outside, path.join(workspace, "escape.txt"));

    const climb = await fetch(`${base}/studio/api/workspace/file?root=workspace&path=..%2F..%2Fetc%2Fpasswd`);
    assert.equal(climb.status, 400);
    assert.match((await climb.json()).error, /climb out/);

    const throughLink = await fetch(`${base}/studio/api/workspace/file?root=workspace&path=escape.txt`);
    assert.equal(throughLink.status, 400);
    assert.match((await throughLink.json()).error, /symlinks/);

    const upload = await fetch(`${base}/studio/api/workspace/file?root=workspace`, {
      method: "POST",
      headers: { "X-Livium-Studio": "1", "X-Livium-Path": "../../outside.txt",
                 "Content-Type": "application/octet-stream" },
      body: Buffer.from("overwrite attempt\n"),
    });
    assert.equal(upload.status, 201);
    // The name is reduced to its last segment, so the write stays in the workspace and
    // the file next door keeps its contents.
    assert.equal((await upload.json()).path, "outside.txt");
    assert.equal(await fs.readFile(outside, "utf8"), "outside\n");

    const missingRoot = await fetch(`${base}/studio/api/workspace?root=nowhere`);
    assert.equal(missingRoot.status, 400);
    assert.match((await missingRoot.json()).error, /unknown root/);
  });
});

test("a directory with contents needs an explicit recursive delete", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    await fs.mkdir(path.join(home, "workspace", "keep", "inner"), { recursive: true });
    await fs.writeFile(path.join(home, "workspace", "keep", "inner", "file.txt"), "x\n");

    const refused = await fetch(`${base}/studio/api/workspace/entry?root=workspace&path=keep`, {
      method: "DELETE", headers: { "X-Livium-Studio": "1" },
    });
    assert.equal(refused.status, 409);
    assert.match((await refused.json()).error, /recursive=1/);
    assert.equal(await fs.stat(path.join(home, "workspace", "keep")).then(() => true), true);

    const removed = await fetch(`${base}/studio/api/workspace/entry?root=workspace&path=keep&recursive=1`, {
      method: "DELETE", headers: { "X-Livium-Studio": "1" },
    });
    assert.equal(removed.status, 200);
    await assert.rejects(() => fs.stat(path.join(home, "workspace", "keep")));
  });
});

test("a missing file is a 404, not a bad request", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base }) => {
    const response = await fetch(`${base}/studio/api/workspace/file?root=workspace&path=nope.txt`);
    assert.equal(response.status, 404);
    assert.match((await response.json()).error, /ENOENT/);
  });
});

test("the page and its assets are served from the Studio itself", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base }) => {
    const page = await fetch(`${base}/`);
    assert.equal(page.status, 200);
    assert.match(page.headers.get("content-type"), /text\/html/);
    const html = await page.text();
    assert.match(html, /\/static\/app\.js/);
    assert.match(html, /\/static\/styles\.css/);
    assert.match(html, /Settings/);

    const script = await fetch(`${base}/static/app.js`);
    assert.equal(script.status, 200);
    assert.match(script.headers.get("content-type"), /javascript/);
    assert.match(await script.text(), /studio\/api\/state/);

    const nowhere = await fetch(`${base}/static/nope.js`);
    assert.equal(nowhere.status, 404);
    // Encoded traversal is not decoded into a path: it is a name that does not exist.
    // The containment check is exercised for real in the workspace tests above.
    const traversal = await fetch(`${base}/static/..%2f..%2fserver.mjs`);
    assert.equal(traversal.status, 404);
    assert.equal((await fs.stat(path.join(REPO_ROOT, "ui", "server.mjs"))).isFile(), true);
  });
});

/** The same, but with the real runtime behind it: needed whenever the payload matters. */
async function withRuntime(t, run) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "livium-studio-live-"));
  await fs.mkdir(path.join(home, "workspace"), { recursive: true });
  await writeSettings(home, { provider: "scripted", extra: { LIVIUM_ALLOW_TEST_DOUBLE: "1" } });
  delete process.env.LIVIUM_STUDIO_NO_BACKEND;
  const studio = await createStudio({ home, port: 0, host: "127.0.0.1", python });
  const address = await studio.listen();
  const base = `http://127.0.0.1:${address.port}`;
  t.after(async () => { await studio.close(); });
  return await run({ base, home });
}

//: (route, key) pairs the Studio's own page reads out of the proxied runtime API. This is
//: the same contract the Python viewer keeps for itself, kept here because the Studio is
//: a second consumer of the same payloads: a renamed field would otherwise show up as a
//: blank panel rather than as an error.
const RUNTIME_FIELDS = [
  ["/api/events?limit=120", ["events"]],
  ["/api/tasks?limit=60", ["tasks"]],
  ["/api/capabilities", ["capabilities", "loaded_capability_tools", "load_failures"]],
  ["/api/discovery?limit=6", ["counts", "records", "cycles", "stopped_cycles", "note"]],
  ["/api/integrity", ["components", "inspection", "enforcement", "frozen", "note"]],
  ["/api/ledger?limit=8", ["rows", "note"]],
  ["/api/memory?query=x", ["records", "stats"]],
  ["/api/recovery", ["findings", "next_action", "safe_to_resume"]],
  ["/api/context?limit=3", ["reports"]],
  ["/api/tools", ["tools"]],
  ["/api/permissions", ["read_roots", "write_roots", "network_allowlist", "denied_binaries",
                        "allowed_binaries", "git_refused"]],
  ["/api/guardian", ["integrity", "frozen", "protected_files", "consequences"]],
];

test("every field the Studio's panels read is in the runtime's answer", { skip: missing, timeout: 120000 },
     async (t) => {
  await withRuntime(t, async ({ base }) => {
    const problems = [];
    for (const [route, keys] of RUNTIME_FIELDS) {
      const response = await fetch(base + route);
      if (!response.ok) {
        problems.push(`${route} answered HTTP ${response.status}`);
        continue;
      }
      const payload = await response.json();
      for (const key of keys) {
        if (!(key in payload)) problems.push(`${route} has no ${key}`);
      }
      if (route === "/api/integrity" && !("status" in (payload.inspection ?? {}))) {
        problems.push("integrity.inspection.status is missing");
      }
    }
    assert.deepEqual(problems, [], "the page and the runtime have drifted apart:\n  " +
                                   problems.join("\n  "));
  });
});

test("the Studio reuses the runtime's probe and can ask for a fresh one", { skip: missing }, async (t) => {
  await withRuntime(t, async ({ base }) => {
    const fresh = await (await fetch(`${base}/studio/api/state?probe=1`)).json();
    assert.equal(fresh.provider.ok, true);
    assert.equal(fresh.provider.fresh, true);
    assert.equal(fresh.provider.age_seconds, 0);
    assert.equal(fresh.provider.is_test_double, true, "the test double must be reported as one");
    assert.equal(fresh.backend.running, true);
    const cached = await (await fetch(`${base}/studio/api/state`)).json();
    assert.equal(cached.provider.fresh, false, "a poll must not probe the provider again");
    assert.equal(cached.provider.age_seconds >= 0, true);
    assert.equal(cached.provider.ok, true);
  });
});

test("an empty objective is refused before anything is spawned", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base }) => {
    const response = await fetch(`${base}/studio/api/chat`, {
      method: "POST", headers: WRITE, body: JSON.stringify({ objective: "   " }),
    });
    assert.equal(response.status, 400);
    assert.match((await response.json()).error, /objective is required/);
    const runs = await (await fetch(`${base}/studio/api/runs`)).json();
    assert.deepEqual(runs.runs, []);
  });
});
