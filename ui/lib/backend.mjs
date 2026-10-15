// The two things the Studio drives: the Python runtime's HTTP backend, and the
// task runs themselves.
//
// The Studio is an operator console, not a second agent. It does not call tools, it
// does not decide anything, and it never reaches into the runtime's own modules:
// everything it shows comes from `livium serve` (a real process, reading the real
// database), and everything it starts is `livium run` (a real process, going through
// the real loop, permission engine, verifier and ledger). If that process cannot
// start, the UI says so instead of drawing a plausible screen.

import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import fs from "node:fs/promises";

const RING_LINES = 400;

function ring(text, line) {
  const lines = `${text ?? ""}${line}`;
  const parts = lines.split("\n");
  return parts.length > RING_LINES ? parts.slice(parts.length - RING_LINES).join("\n") : lines;
}

export async function freePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function waitForHttp(url, { timeoutMs = 20000, intervalMs = 150 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "no attempt was made";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      if (response.ok || response.status === 404) return true;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error.message;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`the runtime did not answer at ${url} within ${timeoutMs} ms: ${lastError}`);
}

/** The `livium serve` process the Studio proxies to. */
export class Backend {
  constructor({ python, repoRoot, home, env }) {
    this.python = python;
    this.repoRoot = repoRoot;
    this.home = home;
    this.env = env;
    this.child = null;
    this.port = null;
    this.log = "";
    this.startedAt = null;
    this.lastError = null;
  }

  get base() {
    return this.port ? `http://127.0.0.1:${this.port}` : null;
  }

  async start() {
    await this.stop();
    this.port = await freePort();
    this.lastError = null;
    this.log = "";
    const args = ["-m", "livium.cli", "serve", "--port", String(this.port), "--host", "127.0.0.1"];
    const child = spawn(this.python, args, {
      cwd: this.repoRoot,
      env: { ...this.env, PYTHONPATH: this.repoRoot },
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.child = child;
    this.startedAt = new Date().toISOString();
    child.stdout.on("data", (chunk) => { this.log = ring(this.log, chunk.toString("utf8")); });
    child.stderr.on("data", (chunk) => { this.log = ring(this.log, chunk.toString("utf8")); });
    child.on("exit", (code, signal) => {
      this.log = ring(this.log, `\n[studio] the runtime backend exited (${code ?? signal})\n`);
      if (this.child === child) this.child = null;
    });
    try {
      await waitForHttp(`${this.base}/api/status`, { timeoutMs: 25000 });
    } catch (error) {
      this.lastError = error.message;
      throw error;
    }
    return { port: this.port, started_at: this.startedAt };
  }

  async stop() {
    const child = this.child;
    this.child = null;
    if (!child || child.exitCode !== null) return;
    await new Promise((resolve) => {
      const done = () => resolve();
      child.once("exit", done);
      child.kill("SIGTERM");
      setTimeout(() => { try { child.kill("SIGKILL"); } catch { /* already gone */ } done(); }, 3000)
        .unref?.();
    });
  }

  get running() {
    return Boolean(this.child && this.child.exitCode === null);
  }

  async json(endpoint, { method = "GET", body = null, timeoutMs = 120000 } = {}) {
    if (!this.running) throw new Error("the runtime backend is not running");
    const response = await fetch(this.base + endpoint, {
      method,
      headers: body === null ? { Accept: "application/json" }
                             : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === null ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await response.text();
    let payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      payload = { error: "the runtime returned a non-JSON body", body: text.slice(0, 500) };
    }
    if (!response.ok) {
      const error = new Error(payload.error || `HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }
}

/** Task runs, each a real `livium run` process with its own task id. */
export class Runs {
  constructor({ python, repoRoot, home, env, makeTaskId }) {
    this.python = python;
    this.repoRoot = repoRoot;
    this.home = home;
    this.env = env;
    this.makeTaskId = makeTaskId;
    this.runs = new Map();
    this.order = [];
    this.limit = 50;
  }

  start({ objective, criteria = null, resume = false, force = false, taskId = null }) {
    const text = String(objective ?? "").trim();
    if (!text) {
      const error = new Error("an objective is required");
      error.status = 400; // the caller's mistake, not a broken Studio
      throw error;
    }
    const task_id = taskId || this.makeTaskId();
    const args = ["-m", "livium.cli", "--json", "run", text];
    if (criteria) args.push("--criteria", typeof criteria === "string" ? criteria : JSON.stringify(criteria));
    if (resume) args.push("--resume");
    if (force) args.push("--force");
    args.push("--task-id", task_id);
    const child = spawn(this.python, args, {
      cwd: this.repoRoot,
      env: { ...this.env, PYTHONPATH: this.repoRoot },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const run = {
      id: `run-${task_id}`,
      task_id,
      objective: text,
      pid: child.pid,
      started_at: new Date().toISOString(),
      finished_at: null,
      status: "running",
      exit_code: null,
      signal: null,
      log: "",
      child,
    };
    const append = (chunk) => { run.log = ring(run.log, chunk.toString("utf8")); };
    child.stdout.on("data", append);
    child.stderr.on("data", append);
    child.on("error", (error) => {
      run.status = "failed";
      run.finished_at = new Date().toISOString();
      run.log = ring(run.log, `\n[studio] could not start the runtime: ${error.message}\n`);
    });
    child.on("exit", (code, signal) => {
      run.status = code === 0 ? "exited" : "failed";
      run.exit_code = code;
      run.signal = signal;
      run.finished_at = new Date().toISOString();
      run.log = ring(run.log, `\n[studio] exit ${code ?? signal}\n`);
    });
    this.runs.set(run.id, run);
    this.order.push(run.id);
    while (this.order.length > this.limit) {
      const oldest = this.order.shift();
      const finished = this.runs.get(oldest);
      if (finished && finished.status === "running") this.order.push(oldest);
      else this.runs.delete(oldest);
    }
    return run;
  }

  /** A run's public shape. The child handle never leaves this process. */
  describe(run) {
    return {
      id: run.id,
      task_id: run.task_id,
      objective: run.objective,
      pid: run.pid,
      started_at: run.started_at,
      finished_at: run.finished_at,
      status: run.status,
      exit_code: run.exit_code,
      signal: run.signal,
      running: run.status === "running",
      log_tail: run.log.slice(-4000),
    };
  }

  list() {
    return this.order.map((id) => this.runs.get(id)).filter(Boolean).reverse()
      .map((run) => this.describe(run));
  }

  get(id) {
    const run = this.runs.get(id);
    return run ? this.describe(run) : null;
  }

  async stop(id) {
    const run = this.runs.get(id);
    if (!run) return null;
    if (run.status !== "running") return this.describe(run);
    return await new Promise((resolve) => {
      run.child.once("exit", () => resolve(this.describe(run)));
      run.child.kill("SIGTERM");
      setTimeout(() => {
        try { run.child.kill("SIGKILL"); } catch { /* already gone */ }
        resolve(this.describe(run));
      }, 4000).unref?.();
    });
  }

  async stopAll() {
    for (const run of this.runs.values()) {
      if (run.status === "running") {
        try { run.child.kill("SIGTERM"); } catch { /* already gone */ }
      }
    }
  }
}

/** Find a usable Python, or say honestly that there is none. */
export async function findPython(preferred) {
  const candidates = [preferred, process.env.LIVIUM_PYTHON, "python3", "python"]
    .filter((value) => typeof value === "string" && value.length > 0);
  for (const candidate of candidates) {
    try {
      const child = spawn(candidate, ["--version"], { stdio: ["ignore", "pipe", "pipe"] });
      const code = await new Promise((resolve) => child.on("exit", resolve));
      if (code === 0) return candidate;
    } catch {
      // try the next candidate
    }
  }
  throw new Error("no Python interpreter was found; set LIVIUM_PYTHON to the one that " +
                  "has this repository installed");
}

export async function repoRootOf(here) {
  const candidate = path.resolve(here, "..");
  await fs.access(path.join(candidate, "livium", "cli.py"));
  return candidate;
}
