// The transcript mapping, against payloads copied out of a real journal.
//
// The chat panel is only worth having if it shows what the runtime recorded. These
// fixtures are the actual event payloads `livium` writes -- `tool_write` carries
// `{path, bytes, sha256, purpose, verified_by_readback}`, `model_reply` carries the
// provider's own report, and so on -- so a change in either side fails here rather than
// silently rendering an empty panel.

import test from "node:test";
import assert from "node:assert/strict";

import { criteriaOf, relativeToWorkspace, transcriptOf, verificationLabel, verificationOf }
  from "../lib/session.mjs";

const ENV = { LIVIUM_WORKSPACE: "/home/operator/.livium/workspace" };

function event(id, type, payload, taskId = "abc123") {
  return { id, type, task_id: taskId, ts: `2026-10-15T09:00:${String(id).padStart(2, "0")}+00:00`,
           payload };
}

// The task row as the runtime stores it: `acceptance` and `summary` are JSON text.
const TASK = {
  id: "abc123",
  objective: "Write report.md in the workspace summarising notes.txt",
  acceptance: JSON.stringify([{ kind: "file_exists", path: "report.md" },
                               { kind: "file_exists", path: "missing.md" }]),
  status: "complete",
  summary: JSON.stringify({
    summary: "wrote report.md from notes.txt", error: null,
    verification: { total: 2, passed: 1, counts: { passed: 1, failed: 1 }, state: "partly verified" },
  }),
};

const EVENTS = [
  event(1, "task_started", { objective: "Write report.md", criteria: [], model: "openai_compatible:gpt-4o-mini" }),
  event(2, "route_decision", { decision: "EXECUTE", reason: "the task has a next action", step: 1, evidence: {} }),
  event(3, "model_reply", { model: "gpt-4o-mini", text: "I will read the notes first.",
                            provider: "openai_compatible", stop_reason: "tool_use", latency_ms: 812,
                            tool_calls: ["read_file"], context_estimated_tokens: 2832,
                            usage: { input_tokens: 1200, output_tokens: 40 },
                            is_test_double: false }),
  event(4, "tool_call", { tool: "read_file", ok: true, category: "read", purpose: "read the source",
                          step: 1 }),
  event(5, "tool_call", { tool: "run_command", ok: false, denied: true, category: "execute",
                          error: "not in the allowlist", step: 1 }),
  event(6, "tool_write", { path: "/home/operator/.livium/workspace/report.md", bytes: 45,
                           sha256: "b97b727706cf5a19", purpose: "the deliverable",
                           verified_by_readback: true }),
  event(7, "boundary_violation", { category: "write", reason: "outside the write roots",
                                   outcome: "refused", approval_id: null }),
  event(8, "task_verified", { total: 2, passed: 1, counts: { passed: 1, failed: 1 },
                              state: "partly verified" }),
  event(9, "task_finished", { status: "complete", summary: "wrote report.md", unverified: ["missing.md"] }),
  event(10, "discovery_captured", { discovery_id: "disc-1", title: "the notes file has no owner" }),
];

test("the transcript is built from the journal and nothing else", () => {
  const payload = transcriptOf("abc123", { task: TASK, steps: [], verification: [] }, EVENTS, ENV);

  assert.deepEqual(payload.messages.map((message) => message.role),
                   ["user", "assistant", "tool", "tool", "artifact", "refusal",
                    "system", "system", "system"]);
  assert.equal(payload.messages[0].body, "Write report.md in the workspace summarising notes.txt");
  assert.equal(payload.messages[1].meta.provider, "openai_compatible");
  assert.equal(payload.messages[1].meta.stop_reason, "tool_use");
  assert.deepEqual(payload.messages[1].meta.tools_requested, ["read_file"]);
  assert.equal(payload.messages[1].meta.tokens_reported, "1200 prompt + 40 completion");
  assert.equal(payload.messages[1].meta.test_double, false);
  assert.equal(payload.messages[2].meta.category, "read");
  assert.equal(payload.messages[2].meta.denied, false);

  // A denial is shown as a refusal with the runtime's own reason, not as a failure of
  // the agent's judgement.
  assert.equal(payload.messages[3].title, "run_command");
  assert.match(payload.messages[3].body, /not in the allowlist/);
  assert.equal(payload.messages[3].meta.denied, true);
  assert.equal(payload.messages[5].title, "write");
  assert.equal(payload.counts.refusals, 2, "one denied call and one boundary violation");
  assert.deepEqual(payload.refusals.map((entry) => entry.source),
                   ["permission engine", "boundary"]);
  assert.equal(payload.counts.events_considered, EVENTS.length);
});

test("a provider that reports no usage is shown as an estimate, not as zero tokens", () => {
  const events = [event(3, "model_reply", { model: "scripted-double", text: "",
                                            provider: "scripted", stop_reason: "tool_use",
                                            usage: { input_tokens: null, output_tokens: null },
                                            context_estimated_tokens: 2832,
                                            is_test_double: true })];
  const payload = transcriptOf("abc123", { task: TASK, steps: [], verification: [] }, events, ENV);
  assert.match(payload.messages[0].meta.tokens_reported, /^~2832 prompt tokens estimated/);
  assert.match(payload.messages[0].meta.tokens_reported, /reported no usage/);
  assert.equal(payload.messages[0].meta.test_double, true);
});

test("a written file is an artifact with the digest the runtime recorded", () => {
  const payload = transcriptOf("abc123", { task: TASK, steps: [], verification: [] }, EVENTS, ENV);
  assert.equal(payload.artifacts.length, 1);
  const [artifact] = payload.artifacts;
  assert.equal(artifact.path, "report.md", "the path is shown relative to the workspace");
  assert.equal(artifact.in_workspace, true);
  assert.deepEqual([artifact.root, artifact.relative_path], ["workspace", "report.md"]);
  assert.equal(artifact.bytes, 45);
  assert.equal(artifact.sha256, "b97b727706cf5a19");
  assert.equal(artifact.purpose, "the deliverable");
  assert.equal(relativeToWorkspace(ENV, "/etc/passwd").in_workspace, false);
});

test("code written in the lab opens in the lab dock, not the workspace", () => {
  const env = { ...ENV, LIVIUM_HOME: "/home/operator/.livium" };
  const journal = [event(1, "tool_write", {
    path: "/home/operator/.livium/capability-lab/line_count/line_count.py",
    bytes: 101, sha256: "ab".repeat(32), verified_by_readback: true,
  })];
  const result = transcriptOf("abc123", { task: TASK, steps: [], verification: [] }, journal, env);
  assert.deepEqual([result.artifacts[0].root, result.artifacts[0].relative_path],
                   ["lab", "line_count/line_count.py"]);
  assert.equal(result.artifacts[0].in_workspace, false);
  assert.equal(result.messages[0].meta.root, "lab");
});

test("the completion summary is read as the runtime's own verdict", () => {
  const state = verificationOf(TASK, { verification: [] });
  assert.equal(state.state, "partly verified");
  assert.equal(state.total, 2);
  assert.equal(state.passed, 1);
  assert.equal(verificationLabel(state), "partly verified 1/2");
  assert.match(state.source, /completion summary/);

  const criteria = criteriaOf(TASK, { verification: [
    { kind: "file_exists", spec: JSON.stringify({ kind: "file_exists", path: "report.md" }),
      outcome: "passed", detail: "/home/operator/.livium/workspace/report.md exists (45 bytes)" },
    { kind: "file_exists", spec: JSON.stringify({ kind: "file_exists", path: "missing.md" }),
      outcome: "failed", detail: "missing.md does not exist" },
  ] });
  assert.equal(criteria.length, 2);
  assert.equal(criteria[0].outcome, "passed");
  assert.equal(criteria[1].outcome, "failed");
  assert.match(criteria[1].detail, /does not exist/);
});

test("a task with no criteria is unverified, not verified-by-default", () => {
  const bare = { id: "x", status: "complete", acceptance: "[]", summary: JSON.stringify({ verification: {} }) };
  const state = verificationOf(bare, { verification: [] });
  assert.notEqual(state.state, "verified");
  assert.equal(verificationLabel(state), "unverified");
  assert.deepEqual(criteriaOf(bare, {}), []);
});

test("a task that never finished has no verdict at all", () => {
  const running = { id: "x", status: "running", acceptance: "[]", summary: null };
  const state = verificationOf(running, { verification: [] });
  assert.equal(state.state, "not run");
  assert.equal(verificationLabel(state), "not run");
  assert.deepEqual(criteriaOf({ acceptance: "not json" }, {}), []);
});

test("steps and their outcomes travel with the transcript", () => {
  const steps = [{ idx: 1, phase: "model_call", status: "complete", detail: "read_file" }];
  const payload = transcriptOf("abc123", { task: TASK, steps, verification: [{ kind: "file_exists" }] },
                               EVENTS, ENV);
  assert.deepEqual(payload.steps, steps);
  assert.equal(payload.verification.length, 1);
  assert.equal(payload.task_id, "abc123");
  assert.equal(payload.task.objective, TASK.objective);
});
