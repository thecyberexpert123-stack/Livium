#!/usr/bin/env node
// Livium Studio: the operator's UI over the Python runtime.
//
// What this process is, precisely:
//
//   * a static server for `ui/public`, and
//   * a same-origin proxy to `livium serve` (a real Python process), and
//   * a small console that writes the provider settings, starts `livium run`
//     processes, and lists/uploads/downloads files inside the runtime's home.
//
// What it is not: a second implementation of the agent. It has no tools, no model
// client and no opinion about a task's outcome. Every fact the UI shows is read from
// the runtime's own API, and every action it takes starts or configures a real
// process. When the runtime cannot start, the UI shows the error and the runtime's
// own log rather than a screen that looks like it works.
//
// Usage:
//   node ui/server.mjs [--port 8877] [--home DIR] [--print-home]

import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { Backend, Runs, findPython, repoRootOf } from "./lib/backend.mjs";
import { listDirectory, PathRefused, readFile, realUnder, safeUploadName } from "./lib/paths.mjs";
import { transcriptOf, verificationLabel, verificationOf } from "./lib/session.mjs";
import {
  childEnvironment, fingerprintOf, protectSettingsFile, publicSettings, readSettings, scrub,
  writeSettings,
} from "./lib/settings.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(HERE, "public");
const MAX_UPLOAD = 64 * 1024 * 1024;
const WRITE_HEADER = "x-livium-studio";

const CONTENT_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
};

function parseArgs(argv) {
  // Localhost by default. The Studio has no authentication and it can write into the
  // runtime's workspace, so widening the binding is a deliberate act (--host 0.0.0.0),
  // not something an operator discovers afterwards.
  const args = { port: 8877, home: null, host: "127.0.0.1", printHome: false };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--port") args.port = Number(argv[++index]);
    else if (value === "--home") args.home = path.resolve(argv[++index]);
    else if (value === "--host") args.host = argv[++index];
    else if (value === "--print-home") args.printHome = true;
  }
  return args;
}

function taskIdOf() {
  return Math.random().toString(16).slice(2, 14).padEnd(12, "0");
}

function json(res, status, payload, { settings } = {}) {
  const body = JSON.stringify(payload, (key, value) =>
    typeof value === "string" && settings ? scrub(value, settings) : value);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  res.end(body);
}

async function readBody(req, limit = MAX_UPLOAD) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new PathRefused(`the body is larger than ${limit} bytes`);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

async function readJson(req) {
  const raw = await readBody(req, 2 * 1024 * 1024);
  if (!raw.length) return {};
  try {
    return JSON.parse(raw.toString("utf8"));
  } catch (error) {
    throw new PathRefused(`the request body is not JSON: ${error.message}`);
  }
}

/** The directories the UI may browse: named, resolved, and nothing else. */
function rootsOf(studio, backendEnv) {
  const home = studio.home;
  const workspace = backendEnv.LIVIUM_WORKSPACE || path.join(home, "workspace");
  return {
    workspace: { label: "workspace", path: workspace },
    home: { label: "runtime home", path: home },
    evidence: { label: "evidence", path: backendEnv.LIVIUM_EVIDENCE || path.join(home, "evidence") },
    lab: { label: "capability lab", path: path.join(home, "capability-lab") },
    promoted: { label: "promoted capabilities", path: backendEnv.LIVIUM_PROMOTED_DIR ||
                                                         path.join(home, "promoted") },
  };
}

async function rootFor(studio, backendEnv, name) {
  const roots = rootsOf(studio, backendEnv);
  const chosen = roots[name ?? "workspace"];
  if (!chosen) {
    throw new PathRefused(`unknown root ${JSON.stringify(name)}; the studio exposes ` +
                          `${Object.keys(roots).join(", ")}`);
  }
  await fs.mkdir(chosen.path, { recursive: true });
  return { name: name ?? "workspace", ...chosen, roots };
}

function eventList(payload) {
  return Array.isArray(payload) ? payload : (payload.events ?? []);
}

export async function createStudio({ home, port = 8877, host = "127.0.0.1", python = null,
                                     repoRoot = null, settings = null } = {}) {
  const repo = repoRoot ?? await repoRootOf(HERE);
  const interpreter = await findPython(python);
  const requestedHome = home ?? process.env.LIVIUM_STUDIO_HOME ?? path.join(repo, ".livium");
  await fs.mkdir(requestedHome, { recursive: true, mode: 0o700 });
  const studioHome = await fs.realpath(requestedHome); // symlinked --home must not bypass key protection
  await protectSettingsFile(studioHome);
  const studio = { home: studioHome, repo, python: interpreter, started_at: new Date().toISOString() };
  // The workspace browser can visit the home root. A JSON preview is scrubbed,
  // but a raw download of settings.json would leak the real key. Refuse every
  // path to that file, for preview, download, upload and deletion alike.
  const secretFile = path.resolve(studioHome, "settings.json");
  const protectSecret = async (absolute, { deletion = false } = {}) => {
    const target = path.resolve(absolute);
    if (target === secretFile || (deletion && secretFile.startsWith(target + path.sep))) {
      throw new PathRefused("settings.json is not a workspace file; use the Settings panel");
    }
    // A second filename can point at the SAME inode. A path-only check would let
    // a hard link in workspace/ stream or overwrite the key, even though the
    // canonical home path was blocked. Do not treat a matching inode as a new file.
    try {
      const [secret, candidate] = await Promise.all([fs.stat(secretFile), fs.stat(target)]);
      if (secret.dev === candidate.dev && secret.ino === candidate.ino) {
        throw new PathRefused("the selected file is the Studio's protected settings file");
      }
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
  };
  let current = settings ?? await readSettings(studioHome);

  const baseEnv = () => {
    const env = childEnvironment(current);
    env.LIVIUM_HOME = studioHome;
    env.LIVIUM_DB = path.join(studioHome, "livium.db");
    env.LIVIUM_WORKSPACE = env.LIVIUM_WORKSPACE || path.join(studioHome, "workspace");
    env.LIVIUM_EVIDENCE = env.LIVIUM_EVIDENCE || path.join(studioHome, "evidence");
    env.LIVIUM_PROMOTED_DIR = env.LIVIUM_PROMOTED_DIR || path.join(studioHome, "promoted");
    return env;
  };

  const backend = new Backend({ python: interpreter, repoRoot: repo, home: studioHome,
                                env: baseEnv() });
  const runs = new Runs({ python: interpreter, repoRoot: repo, home: studioHome,
                          env: baseEnv(), makeTaskId: taskIdOf });

  async function restartBackend() {
    backend.env = baseEnv();
    // The runs read the environment when they start, so they must see the same
    // settings the backend was restarted with. A run that kept the previous key would
    // fail against the provider for a reason the UI could not explain.
    runs.env = backend.env;
    const info = await backend.start();
    let provider = null;
    try {
      // A restart runs the runtime's startup probe, so the answer here is fresh by
      // construction; asking again for a fresh one would be a second request for nothing.
      provider = (await backend.json("/api/status?probe=1")).provider;
    } catch (error) {
      provider = { ok: false, detail: error.message };
    }
    return { backend: describeBackend(), provider };
  }

  function describeBackend() {
    return {
      running: backend.running, port: backend.port, started_at: backend.startedAt,
      last_error: backend.lastError, log_tail: scrub(backend.log, current).slice(-2000),
    };
  }

  async function state({ probe = false } = {}) {
    const payload = {
      studio: { home: studioHome, repo, python: interpreter, node: process.version,
                started_at: studio.started_at, roots: rootsOf(studio, baseEnv()) },
      backend: describeBackend(),
      settings: publicSettings(current),
      key_fingerprint: await fingerprintOf(current),
      runs: runs.list().slice(0, 10),
      provider: null,
      runtime: null,
      error: null,
    };
    if (backend.running) {
      try {
        // The runtime caches its own probe and says how old the answer is; `probe=1` is
        // the operator asking for a fresh one, which is a request and a journal row.
        const status = await backend.json(probe ? "/api/status?probe=1" : "/api/status");
        payload.provider = status.provider;
        payload.runtime = {
          is_test_double: status.provider?.is_test_double ?? null,
          isolation: status.isolation,
          stats: status.status?.stats ?? null,
          memory: status.status?.memory ?? null,
          guardian: status.status?.guardian ?? null,
          config: status.status?.config ?? null,
        };
      } catch (error) {
        payload.error = scrub(error.message, current);
      }
    }
    return payload;
  }

  async function proxy(req, res, url) {
    if (!backend.running) {
      return json(res, 503, { error: "the runtime backend is not running; restart it from " +
                                    "the Studio", backend: describeBackend() }, { settings: current });
    }
    const target = backend.base + url.pathname + (url.search ?? "");
    const headers = { Accept: "application/json" };
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    const method = req.method ?? "GET";
    const response = await fetch(target, {
      method,
      headers: body.length
        ? { ...headers, "Content-Type": req.headers["content-type"] ?? "application/json" }
        : headers,
      body: body.length ? body : undefined,
      signal: AbortSignal.timeout(method === "POST" ? 900000 : 60000),
    });
    const text = await response.text();
    // Scrub decoded JSON values, not only its serialized bytes: a credential with
    // quotes or newlines would be escaped on the wire and miss a raw substring check.
    // Redaction changes the byte count, so frame the *outgoing* body, not the original.
    let outgoing;
    try {
      outgoing = JSON.stringify(JSON.parse(text), (key, value) =>
        typeof value === "string" ? scrub(value, current) : value);
    } catch {
      outgoing = scrub(text, current);
    }
    res.writeHead(response.status, {
      "Content-Type": response.headers.get("content-type") ?? "application/json; charset=utf-8",
      "Content-Length": Buffer.byteLength(outgoing),
      "Cache-Control": "no-store",
    });
    res.end(outgoing);
  }

  async function handleApi(req, res, url) {
    const route = url.pathname.replace(/\/+$/, "") || "/studio/api/state";
    const query = url.searchParams;
    const write = req.method !== "GET" && req.method !== "HEAD";
    if (write && req.headers[WRITE_HEADER] !== "1") {
      return json(res, 403, {
        error: "this endpoint requires the X-Livium-Studio header; the Studio sets it on " +
               "its own requests, and a cross-site form cannot",
      }, { settings: current });
    }

    if (route === "/studio/api/state" && req.method === "GET") {
      return json(res, 200, await state({ probe: query.get("probe") === "1" }),
                  { settings: current });
    }

    if (route === "/studio/api/settings" && req.method === "POST") {
      const patch = await readJson(req);
      current = await writeSettings(studioHome, patch);
      const restarted = patch.restart === false ? null : await restartBackend();
      return json(res, 200, {
        settings: publicSettings(current),
        key_fingerprint: await fingerprintOf(current),
        backend: describeBackend(),
        provider: restarted?.provider ?? null,
        note: patch.restart === false
          ? "settings saved; the running backend still has the previous environment"
          : "settings saved and the runtime restarted with them",
      }, { settings: current });
    }

    if (route === "/studio/api/backend/restart" && req.method === "POST") {
      const result = await restartBackend();
      return json(res, 200, { ...result, backend: describeBackend() }, { settings: current });
    }

    if (route === "/studio/api/backend/log" && req.method === "GET") {
      return json(res, 200, { log: scrub(backend.log, current).slice(-20000),
                              backend: describeBackend() }, { settings: current });
    }

    if (route === "/studio/api/chat" && req.method === "GET") {
      const limit = Number(query.get("limit") ?? 30);
      const result = await backend.json("/api/tasks?limit=" + encodeURIComponent(limit));
      const tasks = (result.tasks ?? []).map((task) => ({
        task_id: task.id, objective: task.objective, status: task.status,
        created_at: task.created_at, updated_at: task.updated_at,
        // The task row keeps the verification counts inside its completion summary;
        // there is no separate column, so the list reads the summary it really has.
        verification: verificationLabel(verificationOf(task)),
        run: runs.list().find((run) => run.task_id === task.id) ?? null,
      }));
      return json(res, 200, { tasks, runs: runs.list() }, { settings: current });
    }

    if (route === "/studio/api/chat" && req.method === "POST") {
      const body = await readJson(req);
      const run = runs.start({
        objective: body.objective, criteria: body.criteria ?? null,
        resume: Boolean(body.resume), force: Boolean(body.force),
        // A resumed task keeps its id, because the runtime's approvals and recovery
        // records are bound to it: a continuation with a fresh id is a different task
        // that happens to have the same objective.
        taskId: body.task_id ? String(body.task_id) : null,
      });
      return json(res, 202, { run: runs.describe(run) }, { settings: current });
    }

    if (route.startsWith("/studio/api/chat/") && req.method === "GET") {
      const taskId = decodeURIComponent(route.split("/").pop());
      const detail = await backend.json(`/api/tasks/${encodeURIComponent(taskId)}`);
      const events = eventList(await backend.json(
        `/api/events?task_id=${encodeURIComponent(taskId)}&limit=500`));
      events.sort((a, b) => (a.id ?? 0) - (b.id ?? 0));
      const payload = transcriptOf(taskId, detail, events, baseEnv());
      payload.run = runs.list().find((entry) => entry.task_id === taskId) ?? null;
      return json(res, 200, payload, { settings: current });
    }

    if (route === "/studio/api/runs" && req.method === "GET") {
      return json(res, 200, { runs: runs.list() }, { settings: current });
    }
    if (route.startsWith("/studio/api/runs/") && route.endsWith("/stop") && req.method === "POST") {
      const id = route.split("/").slice(-2)[0];
      const described = await runs.stop(id);
      if (!described) return json(res, 404, { error: `no run ${id}` }, { settings: current });
      return json(res, 200, { run: described }, { settings: current });
    }
    if (route.startsWith("/studio/api/runs/") && req.method === "GET") {
      const id = decodeURIComponent(route.split("/").pop());
      const described = runs.get(id);
      if (!described) return json(res, 404, { error: `no run ${id}` }, { settings: current });
      return json(res, 200, { run: described }, { settings: current });
    }

    if (route === "/studio/api/workspace" && req.method === "GET") {
      const chosen = await rootFor(studio, baseEnv(), query.get("root"));
      const listing = await listDirectory(chosen.path, query.get("path") ?? "",
                                          { hidden: query.get("hidden") === "1" });
      return json(res, 200, {
        root: { name: chosen.name, label: chosen.label, path: chosen.path },
        roots: Object.entries(chosen.roots).map(([key, value]) => ({ name: key, label: value.label,
                                                                    path: value.path })),
        ...listing,
      }, { settings: current });
    }

    if (route === "/studio/api/workspace/file" && req.method === "GET") {
      const chosen = await rootFor(studio, baseEnv(), query.get("root"));
      const relative = query.get("path") ?? "";
      const info = await readFile(chosen.path, relative);
      await protectSecret(info.absolute);
      if (query.get("download") === "1") {
        const bytes = await fs.readFile(info.absolute);
        res.writeHead(200, {
          "Content-Type": "application/octet-stream",
          "Content-Length": bytes.length,
          "Content-Disposition": `attachment; filename="${encodeURIComponent(info.name)}"`,
          "Cache-Control": "no-store",
        });
        return res.end(bytes);
      }
      const bytes = await fs.readFile(info.absolute);
      const sample = bytes.subarray(0, info.maxBytes);
      const binary = sample.includes(0);
      const sha256 = (await import("node:crypto")).createHash("sha256").update(bytes).digest("hex");
      return json(res, 200, {
        name: info.name, path: relative, size: info.size, modified: info.modified,
        sha256, kind: binary ? "binary" : "text",
        truncated: bytes.length > info.maxBytes,
        content: binary ? null : sample.toString("utf8"),
        note: binary ? "binary file: the Studio serves it for download but does not render it"
                     : null,
      }, { settings: current });
    }

    if (route === "/studio/api/workspace/file" && req.method === "POST") {
      const chosen = await rootFor(studio, baseEnv(), query.get("root"));
      const rawName = req.headers["x-livium-path"] ?? query.get("path") ?? "";
      const subdirectory = String(req.headers["x-livium-directory"] ?? "");
      const name = safeUploadName(decodeURIComponent(String(rawName)));
      const directory = await realUnder(chosen.path, subdirectory);
      await fs.mkdir(directory, { recursive: true });
      const target = await realUnder(chosen.path, path.join(subdirectory, name));
      await protectSecret(target);
      const body = await readBody(req);
      await fs.writeFile(target, body);
      const crypto = await import("node:crypto");
      return json(res, 201, {
        path: path.relative(chosen.path, target), root: chosen.name, name,
        size: body.length,
        sha256: crypto.createHash("sha256").update(body).digest("hex"),
        note: "the file is in the runtime's own workspace, so a task can read it and the " +
              "runtime's permission engine still decides what may be written where",
      }, { settings: current });
    }

    if (route === "/studio/api/workspace/dir" && req.method === "POST") {
      const chosen = await rootFor(studio, baseEnv(), query.get("root"));
      const body = await readJson(req);
      const target = await realUnder(chosen.path, String(body.path ?? ""));
      await fs.mkdir(target, { recursive: true });
      return json(res, 201, { path: path.relative(chosen.path, target), root: chosen.name },
                  { settings: current });
    }

    if (route === "/studio/api/workspace/entry" && req.method === "DELETE") {
      const chosen = await rootFor(studio, baseEnv(), query.get("root"));
      const relative = query.get("path") ?? "";
      const target = await realUnder(chosen.path, relative);
      if (target === path.resolve(chosen.path)) {
        throw new PathRefused("cannot delete a workspace root");
      }
      await protectSecret(target, { deletion: true });
      const stats = await fs.lstat(target);
      if (stats.isDirectory()) {
        const entries = await fs.readdir(target);
        if (entries.length && query.get("recursive") !== "1") {
          return json(res, 409, { error: "the directory is not empty; pass recursive=1 to " +
                                         "remove it and its contents" }, { settings: current });
        }
        await fs.rm(target, { recursive: true, force: true });
      } else {
        await fs.unlink(target);
      }
      return json(res, 200, { removed: relative, root: chosen.name }, { settings: current });
    }

    if (route === "/studio/api/workspace/recent" && req.method === "GET") {
      const chosen = await rootFor(studio, baseEnv(), query.get("root"));
      const minutes = Number(query.get("minutes") ?? 60);
      const since = Date.now() - minutes * 60_000;
      const found = [];
      const walk = async (directory, depth) => {
        if (depth > 6) return;
        const entries = await fs.readdir(directory, { withFileTypes: true });
        for (const entry of entries) {
          if (entry.name.startsWith(".")) continue;
          const absolute = path.join(directory, entry.name);
          const stats = await fs.lstat(absolute);
          if (stats.isDirectory()) await walk(absolute, depth + 1);
          else if (stats.mtimeMs >= since) {
            found.push({
              path: path.relative(chosen.path, absolute), size: stats.size,
              modified: stats.mtime.toISOString(),
            });
          }
        }
      };
      await walk(chosen.path, 0);
      found.sort((a, b) => b.modified.localeCompare(a.modified));
      return json(res, 200, { root: chosen.name, minutes, files: found.slice(0, 200) },
                  { settings: current });
    }

    return json(res, 404, { error: `no studio route ${route}` }, { settings: current });
  }

  async function handleStatic(req, res, url) {
    // The page asks for its own assets under /static/, which is not a directory next to
    // them: the prefix is URL vocabulary and is stripped here. Without this the HTML is
    // served and its stylesheet and script are 404s -- a page that loads and does nothing.
    const bare = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
    const relative = bare === "static" ? "index.html"
      : bare.startsWith("static/") ? bare.slice("static/".length)
      : bare;
    const target = path.resolve(PUBLIC, relative);
    if (target !== PUBLIC && !target.startsWith(PUBLIC + path.sep)) {
      return json(res, 403, { error: "refused a path outside the studio's own assets" });
    }
    try {
      const bytes = await fs.readFile(target);
      const type = CONTENT_TYPES[path.extname(target).toLowerCase()] ?? "application/octet-stream";
      res.writeHead(200, { "Content-Type": type, "Content-Length": bytes.length,
                           "Cache-Control": "no-store" });
      res.end(bytes);
    } catch (error) {
      if (error.code === "ENOENT") {
        return json(res, 404, { error: `${url.pathname} is not part of the studio` });
      }
      throw error;
    }
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);
    try {
      if (url.pathname.startsWith("/api/")) return await proxy(req, res, url);
      if (url.pathname.startsWith("/studio/api/")) return await handleApi(req, res, url);
      return await handleStatic(req, res, url);
    } catch (error) {
      // A refused path is the operator's mistake (400) and a missing file is a normal
      // answer (404). Everything else is a 500 and says what broke, because a Studio that
      // reports a filesystem error as "bad request" teaches the operator the wrong lesson.
      const status = error instanceof PathRefused ? 400
        : error.code === "ENOENT" ? 404
        : error.code === "EISDIR" ? 400
        : error.code === "EACCES" || error.code === "EPERM" ? 403
        : (error.status ?? 500);
      return json(res, status, { error: error.message, kind: error.name, code: error.code },
                  { settings: current });
    }
  });

  return {
    server, studio, backend, runs,
    async listen() {
      if (!backend.running && !process.env.LIVIUM_STUDIO_NO_BACKEND) {
        try {
          await restartBackend();
        } catch (error) {
          backend.lastError = error.message; // reported by /studio/api/state, not swallowed
        }
      }
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, host, resolve);
      });
      return server.address();
    },
    async close() {
      await runs.stopAll();
      await backend.stop();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const studio = await createStudio({ home: args.home, port: args.port, host: args.host });
  if (args.printHome) {
    process.stdout.write(`${studio.studio.home}\n`);
  }
  const address = await studio.listen();
  const shown = address.address === "::" || address.address === "0.0.0.0" ? "0.0.0.0" : address.address;
  process.stdout.write(`\nLivium Studio listening on http://${shown}:${address.port}\n`);
  process.stdout.write(`  runtime home : ${studio.studio.home}\n`);
  process.stdout.write(`  python       : ${studio.studio.python}\n`);
  process.stdout.write(`  backend      : ${studio.backend.running
    ? `livium serve on 127.0.0.1:${studio.backend.port}` : "not running (state will say why)"}\n`);
  process.stdout.write("\nSet the provider and the API key in the Settings panel; nothing is " +
                       "written outside the runtime home.\n");
  if (shown !== "127.0.0.1" && shown !== "localhost" && shown !== "::1") {
    process.stdout.write(`\n  WARNING: bound to ${shown}, and this console has no ` +
                         `authentication. It can write files in the workspace and start ` +
                         `tasks. Bind 127.0.0.1 (the default) unless something in front of ` +
                         `it authenticates the caller.\n`);
  }
  process.stdout.write("\n");

  const shutdown = async () => {
    process.stdout.write("\n[studio] stopping the runtime and any running task\n");
    await studio.close();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main().catch((error) => {
    process.stderr.write(`[studio] ${error.stack ?? error.message}\n`);
    process.exit(1);
  });
}
