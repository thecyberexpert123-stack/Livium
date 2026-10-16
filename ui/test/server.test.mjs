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
import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createStudio } from "../server.mjs";
import { findPython } from "../lib/backend.mjs";
import { writeSettings } from "../lib/settings.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

const KEY = "sk-live-abcdefghijklmnopqrstuvwxyz012345";
const EXTRA_SECRET = "private-credential-with-no-provider-pattern";
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

async function symlinkOrSkip(t, target, link) {
  try {
    await fs.symlink(target, link);
    return true;
  } catch (error) {
    if (process.platform === "win32" && ["EPERM", "EACCES"].includes(error.code)) {
      t.skip("Windows symlink creation requires Developer Mode or elevated privileges");
      return false;
    }
    throw error;
  }
}

test("the state the UI polls carries no key material", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    await fetch(`${base}/studio/api/settings`, {
      method: "POST", headers: WRITE,
      body: JSON.stringify({ api_key: KEY, provider: "anthropic", restart: false,
                             extra: { PRIVATE_DEPLOYMENT_CREDENTIAL: EXTRA_SECRET } }),
    });
    const state = await (await fetch(`${base}/studio/api/state`)).json();
    const text = JSON.stringify(state);
    assert.equal(text.includes(KEY), false, "the key is in the state payload");
    assert.equal(text.includes(KEY.slice(0, 10)), false, "a prefix of the key is in the state payload");
    assert.equal(text.includes(EXTRA_SECRET), false, "a custom extra credential is in the state payload");
    assert.equal(state.settings.extra.PRIVATE_DEPLOYMENT_CREDENTIAL, null);
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
    if (process.platform === "win32") {
      const acl = spawnSync("icacls.exe", [path.join(home, "settings.json")], { encoding: "utf8" });
      assert.equal(acl.status, 0, acl.stderr);
      assert.equal((acl.stdout.match(/:\(F\)/g) ?? []).length, 3,
                   "settings ACL should grant full control only to the account, SYSTEM, and Administrators");
    } else {
      assert.equal((await fs.stat(path.join(home, "settings.json"))).mode & 0o777, 0o600);
    }
  });
});

test("home browsing never returns the key file, even as a raw download", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    await writeSettings(home, { api_key: KEY });
    for (const suffix of ["", "&download=1"]) {
      const response = await fetch(`${base}/studio/api/workspace/file?root=home&path=settings.json${suffix}`);
      assert.equal(response.status, 400);
      assert.equal((await response.text()).includes(KEY), false);
    }
    const upload = await fetch(`${base}/studio/api/workspace/file?root=home`, {
      method: "POST", headers: { "X-Livium-Studio": "1", "X-Livium-Path": "settings.json" },
      body: Buffer.from("overwrite attempt"),
    });
    assert.equal(upload.status, 400);
    for (const location of ["settings.json", ""]) {
      const response = await fetch(`${base}/studio/api/workspace/entry?root=home&path=${location}&recursive=1`, {
        method: "DELETE", headers: { "X-Livium-Studio": "1" },
      });
      assert.equal(response.status, 400);
    }
    // A hard link is another name for the same inode: the path is different but
    // returning its bytes would still expose the key.
    await fs.link(path.join(home, "settings.json"), path.join(home, "workspace", "alias.json"));
    for (const suffix of ["", "&download=1"]) {
      const response = await fetch(`${base}/studio/api/workspace/file?root=workspace&path=alias.json${suffix}`);
      assert.equal(response.status, 400);
      assert.equal((await response.text()).includes(KEY), false);
    }
    const hardlinkUpload = await fetch(`${base}/studio/api/workspace/file?root=workspace`, {
      method: "POST", headers: { "X-Livium-Studio": "1", "X-Livium-Path": "alias.json" },
      body: Buffer.from("overwrite attempt"),
    });
    assert.equal(hardlinkUpload.status, 400);
    assert.equal(JSON.parse(await fs.readFile(path.join(home, "settings.json"), "utf8")).api_key,
                 KEY);
  });
});

test("a symlinked --home is canonicalised before protecting settings.json", { skip: missing }, async (t) => {
  const real = await fs.mkdtemp(path.join(os.tmpdir(), "livium-studio-real-"));
  const alias = `${real}-alias`;
  if (!await symlinkOrSkip(t, real, alias)) return;
  t.after(async () => { await fs.unlink(alias); });
  await writeSettings(real, { api_key: KEY });
  const studio = await createStudio({ home: alias, port: 0, host: "127.0.0.1", python });
  const address = await studio.listen();
  t.after(async () => { await studio.close(); });
  const base = `http://127.0.0.1:${address.port}`;
  assert.equal(studio.studio.home, real);
  const raw = await fetch(`${base}/studio/api/workspace/file?root=home&path=settings.json&download=1`);
  assert.equal(raw.status, 400);
  assert.equal((await raw.text()).includes(KEY), false);
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

    // The browser encodes Unicode filenames into an ASCII header; Node decodes
    // them before validating the name. The accepted path is the one the composer uses.
    const unicode = "résumé plan.txt";
    const named = await fetch(`${base}/studio/api/workspace/file?root=workspace`, {
      method: "POST", headers: { "X-Livium-Studio": "1",
        "X-Livium-Path": encodeURIComponent(unicode), "X-Livium-Directory": "",
        "Content-Type": "application/octet-stream" }, body: Buffer.from("actual contents"),
    });
    assert.equal(named.status, 201);
    assert.equal((await named.json()).path, unicode);
    assert.equal(await fs.readFile(path.join(home, "workspace", unicode), "utf8"), "actual contents");
  });
});

test("paths that climb out or follow a symlink out are refused", { skip: missing }, async (t) => {
  await withStudio(t, async ({ base, home }) => {
    const workspace = path.join(home, "workspace");
    const outside = path.join(home, "outside.txt");
    await fs.writeFile(outside, "outside\n");
    if (!await symlinkOrSkip(t, outside, path.join(workspace, "escape.txt"))) return;

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
    assert.match(html, /id="root"/);
    const assets = [...html.matchAll(/(?:src|href)="(\/static\/assets\/[^" ]+\.(?:js|css))"/g)]
      .map(match => match[1]);
    assert.equal(assets.length, 2, "Vite's production build must reference JS and CSS");
    for (const asset of assets) {
      const response = await fetch(base + asset);
      assert.equal(response.status, 200, `built asset ${asset} is a 404`);
      assert.match(response.headers.get("content-type"), asset.endsWith(".css") ? /text\/css/ : /javascript/);
      if (asset.endsWith(".js")) {
        const script = await response.text();
        assert.match(script, /studio\/api\/state/);
        assert.match(script, /Capabilities/);
      }
    }

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
async function withRuntime(t, run, { script = null, files = {} } = {}) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "livium-studio-live-"));
  const workspace = path.join(home, "workspace");
  await fs.mkdir(workspace, { recursive: true });
  for (const [name, body] of Object.entries(files)) {
    await fs.writeFile(path.join(workspace, name), body);
  }
  const extra = { LIVIUM_ALLOW_TEST_DOUBLE: "1" };
  if (script) {
    const file = path.join(home, "script.json");
    await fs.writeFile(file, JSON.stringify(script));
    extra.LIVIUM_SCRIPT_FILE = file;
  }
  await writeSettings(home, { provider: "scripted", extra });
  delete process.env.LIVIUM_STUDIO_NO_BACKEND;
  const studio = await createStudio({ home, port: 0, host: "127.0.0.1", python });
  const address = await studio.listen();
  const base = `http://127.0.0.1:${address.port}`;
  t.after(async () => { await studio.close(); });
  return await run({ base, home });
}

//: (route, key) pairs this page reads. The Studio is a second consumer of the runtime's
//: payloads, so a renamed field would show up as a blank panel rather than as an error.
//: The list is deliberately the routes the page *actually* calls: a contract test that
//: checks routes nobody reads stops describing the thing it is supposed to protect.
const RUNTIME_FIELDS = [
  ["/studio/api/state", ["settings", "backend", "provider", "runtime", "studio"]],
  ["/studio/api/chat?limit=60", ["tasks", "runs"]],
  ["/studio/api/workspace?root=workspace", ["root", "roots", "entries", "path"]],
  ["/studio/api/workspace/recent?root=workspace&minutes=180", ["files"]],
  ["/api/events?limit=120", ["events"]],
  ["/api/capabilities", ["capabilities", "loaded_capability_tools", "load_failures"]],
  ["/api/discovery?limit=6", ["counts", "records", "cycles", "stopped_cycles", "note"]],
  ["/api/integrity", ["components", "inspection", "enforcement", "frozen", "note"]],
  ["/api/ledger?limit=8", ["rows", "note"]],
];

test("every field the Studio's panels read is in the runtime's answer", { skip: missing, timeout: 120000 },
     async (t) => {
  await withRuntime(t, async ({ base }) => {
    const problems = [];
    const state = await (await fetch(`${base}/studio/api/state`)).json();
    for (const key of ["home", "python", "node", "roots"]) {
      if (!(key in (state.studio ?? {}))) problems.push(`studio state has no studio.${key}`);
    }
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

const SCRIPT = [
  { tool_calls: [{ name: "list_dir", arguments: { path: "." } }] },
  { tool_calls: [{ name: "read_file", arguments: { path: "notes.txt",
                                                   purpose: "read the source material" } }] },
  { tool_calls: [{ name: "write_file", arguments: {
      path: "report.md", purpose: "the deliverable",
      content: "# Report\n\nnotes.txt says: bolts 12, nuts 30.\n" } }] },
  { tool_calls: [{ name: "finish_task", arguments: { summary: "wrote report.md from notes.txt",
                                                      status: "complete" } }] },
];

test("the transcript the page renders is read back from a real run", { skip: missing, timeout: 120000 },
     async (t) => {
  await withRuntime(t, async ({ base }) => {
    const objective = "Write report.md in the workspace summarising notes.txt";
    const started = await (await fetch(`${base}/studio/api/chat`, {
      method: "POST", headers: WRITE,
      body: JSON.stringify({ objective, criteria: [{ kind: "file_exists", path: "report.md" }] }),
    })).json();
    assert.equal(started.run.status, "running");

    const deadline = Date.now() + 90000;
    let run = started.run;
    while (run.status === "running") {
      if (Date.now() > deadline) throw new Error(`the run did not finish:\n${run.log_tail}`);
      await new Promise((resolve) => setTimeout(resolve, 150));
      run = (await (await fetch(`${base}/studio/api/runs/${started.run.id}`)).json()).run;
    }
    assert.equal(run.exit_code, 0, run.log_tail);

    const payload = await (await fetch(`${base}/studio/api/chat/${started.run.task_id}`)).json();
    assert.equal(payload.task.objective, objective);
    assert.equal(payload.verification_label, "verified 1/1");
    assert.equal(payload.criteria[0].outcome, "passed");
    assert.equal(payload.messages[0].role, "user");
    assert.equal(payload.messages[0].body, objective);
    assert.equal(payload.messages[1].role, "assistant");
    assert.equal(payload.counts.artifacts, 1);

    const [artifact] = payload.artifacts;
    assert.equal(artifact.path, "report.md");
    assert.equal(artifact.in_workspace, true);
    assert.match(artifact.sha256, /^[0-9a-f]{64}$/);
    assert.equal(artifact.bytes, 45);
    assert.equal(payload.steps.length > 0, true, "a real run records steps");
    assert.equal(payload.run.status, "exited");

    // ...and the sidebar's own list carries the same verdict, read from the task row.
    const list = await (await fetch(`${base}/studio/api/chat?limit=60`)).json();
    const row = list.tasks.find((task) => task.task_id === started.run.task_id);
    assert.ok(row, "the task is missing from the sidebar list");
    assert.equal(row.verification, "verified 1/1");
    assert.equal(row.status, "complete");
  }, { script: SCRIPT, files: { "notes.txt": "bolts: 12\nnuts: 30\n" } });
});

test("proxied JSON redacts ordinary and escaped credentials with correct framing", {
  skip: missing, timeout: 120000,
}, async (t) => {
  await withRuntime(t, async ({ base }) => {
    const escapedKey = 'sk-live-a"b\\cdefghijklmnopqrst';
    const saved = await fetch(`${base}/studio/api/settings`, {
      method: "POST", headers: WRITE,
      body: JSON.stringify({ api_key: escapedKey, extra: { CUSTOM_OPTION: EXTRA_SECRET },
                             restart: false }),
    });
    assert.equal(saved.status, 200);
    const response = await fetch(`${base}/studio/api/chat`, {
      method: "POST", headers: WRITE,
      body: JSON.stringify({ objective: `Summarise ${escapedKey} and ${EXTRA_SECRET}`,
                             criteria: [{ kind: "event", event_type: "tool_call" }] }),
    });
    assert.equal(response.status, 202);
    const started = (await response.json()).run;
    const deadline = Date.now() + 30000;
    let run = started;
    while (run.running) {
      if (Date.now() > deadline) throw new Error(`run did not finish: ${run.log_tail}`);
      await new Promise(resolve => setTimeout(resolve, 150));
      run = (await (await fetch(`${base}/studio/api/runs/${run.id}`)).json()).run;
    }
    assert.equal(run.exit_code, 0, run.log_tail);
    const proxied = await fetch(`${base}/api/tasks?limit=10`);
    const text = await proxied.text();
    assert.equal(proxied.status, 200);
    assert.equal(Number(proxied.headers.get("content-length")), Buffer.byteLength(text),
                 "redaction must remeasure the outgoing response, not the incoming one");
    assert.equal(text.includes(escapedKey), false);
    assert.equal(text.includes(EXTRA_SECRET), false);
    const task = JSON.parse(text).tasks.find(item => item.id === started.task_id);
    assert.ok(task, "the proxied task list is still valid JSON");
    assert.match(task.objective, /redacted/);
  }, { script: [{ tool_calls: [{ name: "finish_task", arguments: {
    summary: "synthetic redaction test completed", status: "complete",
  } }] }] });
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

// This is not a test of model inventiveness: the scripted double supplies the tool
// calls. All files, tests, digests, CLI decisions, imports and invocations are real.
// It proves a browser-started task can develop and validate a general tool, that
// the read-only review is honest, and that a separate human CLI step makes it
// available on a later run (never in the same run before promotion).
test("Studio → agent lab → tests → review → human CLI → new usable tool", {
  skip: missing, timeout: 120000,
}, async (t) => {
  const module = `from livium.tools import ToolResult
CAPABILITY = {
  "name": "text_upper", "requirement": "uppercase text without a shell",
  "description": "Return the upper-case form of text",
  "tools": [{"name": "text_uppercase", "description": "Uppercase the supplied text",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"]}}],
}
def handle(tool_name, args, ctx):
    return ToolResult(tool=tool_name, ok=True, output=str(args.get("text", "")).upper())
`;
  const tests = `import unittest
import text_upper
class ActualBehaviour(unittest.TestCase):
    def test_uppercase_output(self):
        result = text_upper.handle("text_uppercase", {"text": "hello 42"}, None)
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "HELLO 42")
    def test_empty_text(self):
        self.assertEqual(text_upper.handle("text_uppercase", {"text": ""}, None).output, "")
`;
  const finish = { tool_calls: [{ name: "finish_task", arguments: {
    summary: "validated text_upper; waiting for a named human to promote it",
    status: "complete",
  } }] };
  await withRuntime(t, async ({ base, home }) => {
    const lab = path.join(home, "capability-lab", "text_upper");
    await fs.writeFile(path.join(home, "script.json"), JSON.stringify([
      { tool_calls: [{ name: "request_capability", arguments: {
        name: "text_upper", requirement: "uppercase text without a shell",
        reuse_analysis: "No registered tool transforms supplied text into uppercase",
        verification_plan: "assert output for real input and empty input",
      } }] },
      { tool_calls: [{ name: "capability_scaffold", arguments: { name: "text_upper" } }] },
      { tool_calls: [{ name: "write_file", arguments: {
        path: path.join(lab, "text_upper.py"), content: module, purpose: "the real implementation",
      } }] },
      { tool_calls: [{ name: "write_file", arguments: {
        path: path.join(lab, "test_text_upper.py"), content: tests, purpose: "real assertions",
      } }] },
      { tool_calls: [{ name: "capability_test", arguments: { name: "text_upper" } }] },
      finish,
    ]));

    async function runTask(objective, eventType) {
      const response = await fetch(`${base}/studio/api/chat`, {
        method: "POST", headers: WRITE,
        body: JSON.stringify({ objective, criteria: [{ kind: "event", event_type: eventType }] }),
      });
      if (response.status !== 202) assert.fail(`start failed: ${await response.text()}`);
      const started = await response.json();
      const deadline = Date.now() + 90000;
      let current = started.run;
      while (current.running) {
        if (Date.now() > deadline) throw new Error(`the run did not finish: ${current.log_tail}`);
        await new Promise(resolve => setTimeout(resolve, 150));
        current = (await (await fetch(`${base}/studio/api/runs/${current.id}`)).json()).run;
      }
      assert.equal(current.exit_code, 0, current.log_tail);
      return await (await fetch(`${base}/studio/api/chat/${started.run.task_id}`)).json();
    }

    const drafted = await runTask("Build and validate a reusable uppercase capability",
                                  "capability_tested");
    assert.equal(drafted.verification_label, "verified 1/1");
    assert.equal(drafted.counts.artifacts, 2);
    assert.equal(drafted.artifacts[0].root, "lab");
    assert.equal(drafted.artifacts[0].relative_path, "text_upper/text_upper.py");
    const list = await (await fetch(`${base}/api/capabilities`)).json();
    const candidate = list.capabilities.find(item => item.name === "text_upper");
    assert.equal(candidate.status, "validated");
    assert.equal(candidate.promoted_by, null);
    assert.equal(candidate.validated_manifest["text_upper.py"],
                 crypto.createHash("sha256").update(module).digest("hex"));
    const toolsBefore = await (await fetch(`${base}/api/tools`)).json();
    assert.equal(toolsBefore.tools.some(tool => tool.name === "text_uppercase"), false);
    const reviewBefore = await (await fetch(`${base}/api/capabilities/text_upper/review`)).json();
    assert.equal(reviewBefore.lab.unchanged, true);
    assert.equal(reviewBefore.eligible_for_human_review, true);
    assert.equal(reviewBefore.guardian_gate.ok, false, "no human baseline has been recorded yet");

    const env = { ...process.env, PYTHONPATH: REPO_ROOT,
      LIVIUM_HOME: home, LIVIUM_DB: path.join(home, "livium.db"),
      LIVIUM_WORKSPACE: path.join(home, "workspace"),
      LIVIUM_PROMOTED_DIR: path.join(home, "promoted"),
      LIVIUM_PROVIDER: "scripted", LIVIUM_ALLOW_TEST_DOUBLE: "1",
      LIVIUM_SCRIPT_FILE: path.join(home, "script.json") };
    function humanCli(...args) {
      const result = spawnSync(python, ["-m", "livium.cli", ...args], {
        cwd: REPO_ROOT, env, timeout: 25000, encoding: "utf8",
      });
      assert.equal(result.status, 0, `${args.join(" ")} failed:\n${result.stderr}\n${result.stdout}`);
    }
    humanCli("guardian", "baseline", "--approved-by", "Test Operator");
    humanCli("capability", "review", "text_upper");
    humanCli("capability", "promote", "text_upper", "--approved-by", "Test Operator");
    const activated = await (await fetch(`${base}/api/capabilities/text_upper/review`)).json();
    assert.equal(activated.capability.status, "active");
    assert.equal(activated.capability.promoted_by, "Test Operator");
    assert.equal(activated.promoted.unchanged, true);

    await fs.writeFile(path.join(home, "script.json"), JSON.stringify([
      { tool_calls: [{ name: "text_uppercase", arguments: { text: "hello 42" } }] },
      { tool_calls: [{ name: "finish_task", arguments: {
        summary: "used the new tool", status: "complete",
      } }] },
    ]));
    const reused = await runTask("Use text_uppercase on hello 42", "capability_tool_used");
    assert.equal(reused.verification_label, "verified 1/1");
    assert.equal(reused.messages.some(message => message.title === "text_uppercase" &&
      message.meta?.ok === true), true, "new tool did not actually run");
    const after = await (await fetch(`${base}/api/capabilities/text_upper/review`)).json();
    assert.equal(after.capability.status, "maintained");
    humanCli("capability", "deprecate", "text_upper", "--approved-by", "Test Operator",
             "--reason", "superseded in test");
    const deprecated = await (await fetch(`${base}/api/capabilities/text_upper/review`)).json();
    assert.equal(deprecated.capability.status, "deprecated");
    assert.equal(deprecated.promoted.observed, null, "retired tools must not remain installed");
    humanCli("capability", "rollback", "text_upper", "--reason", "test completed");
    const withdrawn = await (await fetch(`${base}/api/capabilities/text_upper/review`)).json();
    assert.equal(withdrawn.capability.status, "rollback");
    assert.equal(withdrawn.promoted.observed, null);
  }, { script: [finish] });
});
