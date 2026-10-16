// A run that really spawns the runtime.
//
// The Studio does not implement an agent: it starts `livium run` as a child process and
// shows what the runtime recorded. So the test for "the Run button works" is a real
// child process, a real task row and a real file on disk -- driven by the scripted test
// double, which is the runtime's own mechanism for a run with no model behind it. The
// replies are scripted; the loop, the tools, the permission engine, the verification and
// the journal are not.

import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { Runs, findPython, repoRootOf } from "../lib/backend.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = await repoRootOf(path.join(HERE, "..")); // asserts ui/ sits in the repository

const python = await findPython().catch(() => null);
const missing = python ? false : "no Python interpreter on PATH; there is no runtime to run";

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

async function scratchHome() {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "livium-run-"));
  const workspace = path.join(home, "workspace");
  await fs.mkdir(workspace, { recursive: true });
  await fs.writeFile(path.join(workspace, "notes.txt"), "bolts: 12\nnuts: 30\n");
  const script = path.join(home, "script.json");
  await fs.writeFile(script, JSON.stringify(SCRIPT));
  return { home, workspace, script };
}

function environment(home, script) {
  return {
    ...process.env,
    LIVIUM_HOME: home,
    LIVIUM_DB: path.join(home, "livium.db"),
    LIVIUM_WORKSPACE: path.join(home, "workspace"),
    LIVIUM_EVIDENCE: path.join(home, "evidence"),
    LIVIUM_PROVIDER: "scripted",
    LIVIUM_ALLOW_TEST_DOUBLE: "1",
    LIVIUM_SCRIPT_FILE: script,
    PYTHONPATH: REPO,
  };
}

async function waitFor(runs, id, { timeoutMs = 60000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const run = runs.get(id);
    if (run && run.status !== "running") return run;
    if (Date.now() > deadline) {
      throw new Error(`the run did not finish within ${timeoutMs} ms:\n${run?.log_tail ?? ""}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

test("Runs refuses an objective it cannot run, before spawning anything", () => {
  const runs = new Runs({ python: "python3", repoRoot: REPO, home: "/tmp", env: {} });
  assert.throws(() => runs.start({ objective: "   " }), /objective is required/);
  assert.deepEqual(runs.list(), []);
});

test("a task started through the Studio really runs and writes its file", { skip: missing, timeout: 120000 },
     async () => {
  const { home, workspace, script } = await scratchHome();
  const runs = new Runs({ python, repoRoot: REPO, home, env: environment(home, script),
                          makeTaskId: () => "studio-test-task" });
  try {
    const started = runs.start({
      objective: "Write report.md in the workspace summarising notes.txt",
      criteria: [{ kind: "file_exists", path: "report.md" }],
    });
    assert.equal(started.task_id, "studio-test-task");
    assert.equal(started.status, "running");

    const finished = await waitFor(runs, started.id);
    assert.equal(finished.status, "exited", `run failed:\n${finished.log_tail}`);
    assert.equal(finished.exit_code, 0);
    assert.equal(finished.running, false);

    const written = await fs.readFile(path.join(workspace, "report.md"), "utf8");
    assert.match(written, /bolts 12, nuts 30/);
    assert.equal(await fs.readFile(path.join(workspace, "notes.txt"), "utf8"), "bolts: 12\nnuts: 30\n");

    // The runtime says out loud that a test double answered, and that must survive all
    // the way into the log the Studio shows.
    assert.match(finished.log_tail, /test double/);
    assert.match(finished.log_tail, /No real model/i);
    assert.equal((await fs.stat(path.join(home, "livium.db"))).size > 0, true);
  } finally {
    await runs.stopAll();
  }
});

test("the runtime's own verdict on the criteria is verified, not claimed", { skip: missing, timeout: 120000 },
     async () => {
  const { home, workspace, script } = await scratchHome();
  const output = await new Promise((resolve) => {
    execFile(python, ["-m", "livium.cli", "--json", "run",
                      "Write report.md in the workspace summarising notes.txt",
                      "--criteria", JSON.stringify([{ kind: "file_exists", path: "report.md" }]),
                      "--task-id", "studio-verdict-task"],
             { cwd: REPO, env: environment(home, script), maxBuffer: 32 * 1024 * 1024 },
             (error, stdout, stderr) => resolve({ error, stdout, stderr }));
  });
  assert.equal(output.error, null, output.stderr);
  const report = JSON.parse(output.stdout);
  assert.equal(report.task_id, "studio-verdict-task");
  assert.equal(report.status, "complete");
  assert.equal(report.verification.state, "verified");
  assert.equal(report.verification.total, 1);
  assert.equal(report.verification.counts.passed, 1);
  assert.equal(report.is_test_double, true);
  assert.equal((await fs.stat(path.join(workspace, "report.md"))).size > 0, true);
});
