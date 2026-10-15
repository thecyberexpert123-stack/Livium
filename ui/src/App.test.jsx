// Component tests exercise the compiled React component tree and its network
// boundary. Payloads are explicit UI fixtures, NOT evidence that a model worked;
// ui/test/server.test.mjs runs the real Node/Python API with a labelled test double.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App.jsx";

const state = {
  studio: { home: "/tmp/test-studio", python: "python3", roots: {
    workspace: { label: "workspace", path: "/tmp/test-studio/workspace" },
    lab: { label: "capability lab", path: "/tmp/test-studio/capability-lab" },
    promoted: { label: "promoted capabilities", path: "/tmp/test-studio/promoted" },
  } },
  backend: { running: true, port: 12345, last_error: null },
  settings: { provider: "scripted", model: "scripted-double", base_url: "", api_key_set: true,
              api_key_hint: "set (36 characters)", extra: {}, warnings: ["scripted test double"] },
  key_fingerprint: "abcd1234abcd",
  provider: { ok: true, detail: "NO REAL MODEL", age_seconds: 12, fresh: false,
              model: "scripted-double", is_test_double: true },
  runtime: { isolation: { network_isolated: true, filesystem_confined: false,
                           detail: "network isolated; filesystem NOT confined" } },
};
const run = { id: "run-1", task_id: "t1", running: false, status: "exited", exit_code: 0,
  pid: 123, started_at: "2026-10-15T09:00:00Z", finished_at: "2026-10-15T09:00:04Z",
  log_tail: "task t1 [complete]" };
const tasks = { tasks: [{ task_id: "t1", objective: "Write report.md from notes.txt",
  verification: "verified 1/1", status: "complete", run }], runs: [run] };
const detail = { task_id: "t1", task: { objective: tasks.tasks[0].objective }, run,
  counts: { messages: 5, artifacts: 1, refusals: 0 },
  verification_state: { state: "verified", source: "the task's own completion summary",
                        total: 1, passed: 1 }, verification_label: "verified 1/1",
  criteria: [{ spec: { kind: "file_exists", path: "report.md" }, outcome: "passed",
               detail: "read-back confirmed" }],
  messages: [
    { role: "user", body: tasks.tasks[0].objective },
    { role: "assistant", title: "scripted-double", body: "I will read it first.",
      meta: { test_double: true, tokens_reported: "provider reported no usage" } },
    { role: "artifact", title: "report.md", body: "wrote 45 bytes", meta: {
      in_workspace: true, root: "workspace", relative_path: "report.md" } },
    { role: "system", title: "note", body: "NOTE: this run used the scripted test double. No real model." },
    { role: "system", title: "task finished", body: "report.md written" },
  ], artifacts: [{ path: "report.md", root: "workspace", relative_path: "report.md",
    in_workspace: true, bytes: 45, sha256: "a".repeat(64) }],
  steps: [{ idx: 1, phase: "execute", status: "completed", detail: "write_file" }],
};
const file = { path: "report.md", name: "report.md", kind: "text", size: 45,
               modified: "2026-10-15T09:00:00Z", sha256: "a".repeat(64),
               truncated: false, content: "# Report\n\nbolts 12, nuts 30\n" };
const capability = { name: "line_count", status: "validated", requirement: "count non-blank lines",
  reuse_analysis: "list_dir cannot count lines", promoted_by: null,
  verification: "tests passed; source and test digests recorded", evidence: "exit_code=0",
  tools: [{ name: "line_count_count", description: "Count non-blank lines" }] };
const review = { capability, lab: { expected_sha256: "a".repeat(64),
  observed_sha256: "a".repeat(64), unchanged: true,
  expected: { "line_count.py": "b".repeat(64), "test_line_count.py": "c".repeat(64) } },
  promoted: { expected_sha256: null, observed_sha256: null, unchanged: false },
  guardian_gate: { ok: true, detail: "integrity intact" }, eligible_for_human_review: true,
  note: "Passing tests prove only what the tests asserted." };

let calls;
function backend(url, options = {}) {
  calls.push({ url: String(url), options });
  let result;
  if (options.method === "POST" && String(url).includes("/studio/api/settings")) {
    result = { settings: { ...state.settings, provider: "ollama" }, backend: state.backend,
      provider: { ...state.provider, ok: true }, note: "settings saved and restarted" };
  } else if (options.method === "POST" && String(url) === "/studio/api/chat") {
    result = { run: { id: "run-new", task_id: "new-task", status: "running", running: true } };
  } else if (String(url) === "/studio/api/state") result = state;
  else if (String(url) === "/studio/api/chat?limit=60") result = tasks;
  else if (String(url) === "/studio/api/chat/t1") result = detail;
  else if (String(url) === "/studio/api/chat/new-task") {
    return Promise.resolve(new Response(JSON.stringify({ error: "not yet journalled" }), {
      status: 404, headers: { "Content-Type": "application/json" },
    }));
  } else if (String(url) === "/studio/api/runs/run-1") result = { run };
  else if (String(url) === "/studio/api/runs/run-new") {
    result = { run: { id: "run-new", task_id: "new-task", status: "running", running: true } };
  } else if (String(url).startsWith("/studio/api/workspace?")) {
    result = { root: { name: "workspace", path: state.studio.roots.workspace.path },
      entries: [{ name: "report.md", path: "report.md", kind: "file", size: 45 }], path: "" };
  } else if (String(url).startsWith("/studio/api/workspace/recent?")) {
    result = { files: [{ path: "report.md", size: 45 }] };
  } else if (String(url).startsWith("/studio/api/workspace/file?")) result = file;
  else if (String(url) === "/studio/api/backend/log") result = { log: "runtime ready" };
  else if (String(url) === "/api/capabilities") result = {
    capabilities: [capability], loaded_capability_tools: [], load_failures: [] };
  else if (String(url) === "/api/capabilities/line_count/review") result = review;
  else result = { error: `unexpected fixture URL ${url}` };
  return Promise.resolve(new Response(JSON.stringify(result), {
    status: result.error ? 404 : 200, headers: { "Content-Type": "application/json" },
  }));
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
}

beforeEach(() => {
  calls = [];
  localStorage.clear();
  Object.defineProperty(window, "innerWidth", { value: 1440, configurable: true });
  vi.stubGlobal("fetch", vi.fn(backend));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("the actual React UI", () => {
  it("renders the real health state, task history, and a controlled composer", async () => {
    mount();
    expect(await screen.findByText("Write report.md from notes.txt")).toBeTruthy();
    expect(screen.getByText(/scripted test double · no real model/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Build a missing capability/ }));
    expect(screen.getByRole("textbox", { name: "Task objective" }).value).toMatch(/Stop at Validated/);
    fireEvent.click(screen.getByRole("button", { name: /Theme/ }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("livium.theme")).toBe("light");
  });

  it("renders journalled messages and the actual verification, and opens an artifact", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Write report.md from notes.txt/ }));
    const transcript = await screen.findByLabelText("Task transcript");
    expect(await within(transcript).findByText("I will read it first.")).toBeTruthy();
    expect(within(screen.getByLabelText("Acceptance checks")).getByText("verified 1/1")).toBeTruthy();
    expect(within(transcript).queryByText(/NOTE: this run used/)).toBeNull();
    fireEvent.click(within(transcript).getByRole("button", { name: "Open" }));
    expect(await screen.findByText(/bolts 12, nuts 30/)).toBeTruthy();
    expect(calls.some(call => call.url.includes("workspace/file?root=workspace&path=report.md"))).toBe(true);
  });

  it("sends a real task through the Node route with criteria and the write guard", async () => {
    mount();
    fireEvent.change(screen.getByRole("textbox", { name: "Task objective" }),
                     { target: { value: "Count the bolts in notes.txt" } });
    fireEvent.click(screen.getByRole("button", { name: /Acceptance criteria & options/ }));
    fireEvent.change(screen.getByPlaceholderText(/file_exists/),
                     { target: { value: '[{"kind":"file_exists","path":"report.md"}]' } });
    fireEvent.click(screen.getByRole("button", { name: "Start the task" }));
    await waitFor(() => expect(calls.some(call => call.url === "/studio/api/chat" &&
      call.options.method === "POST")).toBe(true));
    const post = calls.find(call => call.url === "/studio/api/chat" && call.options.method === "POST");
    expect(post.options.headers["X-Livium-Studio"]).toBe("1");
    expect(JSON.parse(post.options.body)).toMatchObject({ objective: "Count the bolts in notes.txt",
      criteria: [{ kind: "file_exists", path: "report.md" }] });
    expect(await screen.findByText(/Started a real Livium run/)).toBeTruthy();
  });

  it("keeps the API key out of responses and sends it only to Settings", async () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: /Settings/ }));
    const field = await screen.findByLabelText("API key");
    fireEvent.change(field, { target: { value: "sk-temporary-unit-test-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save and restart the runtime" }));
    await waitFor(() => expect(calls.some(call => call.url === "/studio/api/settings" &&
      call.options.method === "POST")).toBe(true));
    const post = calls.find(call => call.url === "/studio/api/settings" && call.options.method === "POST");
    expect(post.options.headers["X-Livium-Studio"]).toBe("1");
    expect(JSON.parse(post.options.body).api_key).toBe("sk-temporary-unit-test-secret");
    await waitFor(() => expect(field.value).toBe(""));
    expect(document.body.textContent).not.toContain("sk-temporary-unit-test-secret");
  });

  it("shows exact tested files, Guardian decision and the CLI-only human gate", async () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: /Capabilities/ }));
    const dialog = screen.getByRole("dialog", { name: "Capabilities" });
    expect(await within(dialog).findByText("exact tested files still present")).toBeTruthy();
    expect(within(dialog).getByText(/integrity intact/)).toBeTruthy();
    expect(within(dialog).getByText(/--approved-by/)).toBeTruthy();
    expect(within(dialog).queryByRole("button", { name: /promote/i })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "test_line_count.py" }));
    expect(screen.queryByRole("dialog", { name: "Capabilities" })).toBeNull();
    await waitFor(() => expect(calls.some(call => call.url.includes("root=lab&path=line_count%2Ftest_line_count.py"))).toBe(true));
  });
});
