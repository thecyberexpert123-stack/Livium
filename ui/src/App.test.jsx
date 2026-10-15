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
let stateFixture;
let detailFixture;
let uploadFixture;
let chatError;
function backend(url, options = {}) {
  calls.push({ url: String(url), options });
  let result;
  if (options.method === "POST" && String(url).includes("/studio/api/settings")) {
    result = { settings: { ...state.settings, provider: "ollama" }, backend: state.backend,
      provider: { ...state.provider, ok: true }, note: "settings saved and restarted" };
  } else if (options.method === "POST" && String(url) === "/studio/api/chat") {
    result = chatError ? { error: chatError } :
      { run: { id: "run-new", task_id: "new-task", status: "running", running: true } };
  } else if (String(url) === "/studio/api/state") result = stateFixture;
  else if (String(url) === "/studio/api/chat?limit=60") result = tasks;
  else if (String(url) === "/studio/api/chat/t1") result = detailFixture;
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
  } else if (options.method === "POST" && String(url).startsWith("/studio/api/workspace/file?")) {
    result = uploadFixture;
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
  stateFixture = state;
  detailFixture = detail;
  uploadFixture = { path: "accepted-notes.txt", name: "accepted-notes.txt", root: "workspace" };
  chatError = null;
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
    expect(document.documentElement.dataset.theme).toBe("light");
    fireEvent.click(screen.getByRole("button", { name: /Theme/ }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("livium.theme")).toBe("dark");
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

  it("is chat-first: filters real history and opens the file browser only when asked", async () => {
    const { container } = mount();
    expect(await screen.findByRole("button", { name: /Write report.md from notes.txt/ })).toBeTruthy();
    expect(container.querySelector(".app").classList.contains("dock-hidden")).toBe(true);
    expect(screen.getByText(/New task per send \(unless resuming\)/)).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter recent tasks" }),
      { target: { value: "not in this history" } });
    expect(screen.queryByRole("button", { name: /Write report.md from notes.txt/ })).toBeNull();
    expect(screen.getByText(/No recent tasks match/)).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter recent tasks" }),
      { target: { value: "report" } });
    fireEvent.click(screen.getByRole("button", { name: "Show or hide the workspace" }));
    expect(container.querySelector(".app").classList.contains("dock-open")).toBe(true);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(container.querySelector(".app").classList.contains("dock-hidden")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /Write report.md from notes.txt/ }));
    expect(await screen.findByLabelText("Task transcript")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(screen.queryByLabelText("Task transcript")).toBeNull();
    expect(screen.getByRole("heading", { name: /What can I help you/ })).toBeTruthy();
  });

  it("does not call an unreachable real provider a working AI connection", async () => {
    stateFixture = { ...state, settings: { ...state.settings, provider: "openai_compatible",
      model: "real-model", warnings: [], api_key_set: false },
      provider: { ok: false, is_test_double: false,
        detail: "HTTP 401: this model could not be reached" } };
    mount();
    expect(await screen.findByText("Model connection needs attention")).toBeTruthy();
    expect(screen.getByText(/HTTP 401: this model could not be reached/)).toBeTruthy();
    expect(screen.queryByText(/scripted test double · no real model/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
    expect(screen.getByRole("dialog", { name: "Settings" })).toBeTruthy();
  });

  it("treats an unprobed provider as unknown, not as a demo or a successful connection", async () => {
    stateFixture = { ...state, settings: { ...state.settings, provider: "ollama",
      model: "local-model", warnings: [] }, provider: null };
    mount();
    expect(await screen.findByText("Checking the model connection")).toBeTruthy();
    expect(screen.getByText("Checking provider")).toBeTruthy();
    expect(screen.queryByText("Demo mode, not live AI")).toBeNull();
    expect(screen.queryByText("Ready to chat")).toBeNull();
  });

  it("safely renders model Markdown but not remote images or executable HTML", async () => {
    detailFixture = { ...detail, messages: [detail.messages[0], {
      role: "assistant", title: "scripted-double", meta: { test_double: true },
      body: ["## Review", "", "- **done**", "- [safe](https://example.org)",
        "- [bad](javascript:alert(1))", "", "![remote](https://example.org/tracker.png)",
        "", "<script>alert('x')</script>", "", "```js", "const ok = true;", "```"].join("\n"),
    }] };
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Write report.md from notes.txt/ }));
    const transcript = await screen.findByLabelText("Task transcript");
    expect(within(transcript).getByRole("heading", { name: "Review" })).toBeTruthy();
    const link = within(transcript).getByRole("link", { name: "safe" });
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(transcript.querySelector("script, img")).toBeNull();
    expect(within(transcript).getByText(/External image omitted: remote/)).toBeTruthy();
    const dangerous = within(transcript).getByText("bad").closest("a");
    expect(dangerous?.getAttribute("href") ?? "").not.toMatch(/^javascript:/);
    expect(within(transcript).getByRole("button", { name: "Copy code" })).toBeTruthy();
  });

  it("keeps recorded tool activity inspectable and refusals plainly visible", async () => {
    detailFixture = { ...detail, messages: [detail.messages[0],
      { role: "assistant", body: "I'll check the file.", meta: { test_double: true } },
      { role: "tool", title: "read_file", body: "ok", meta: { ok: true, step: 1 } },
      { role: "tool", title: "run_command", body: "refused: not allowed",
        meta: { denied: true, step: 2 } },
      { role: "refusal", title: "permission engine", body: "write outside workspace",
        meta: { outcome: "denied" } },
      detail.messages.at(-1)] };
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Write report.md from notes.txt/ }));
    const transcript = await screen.findByLabelText("Task transcript");
    const activity = transcript.querySelector(".activity-group");
    expect(activity.open).toBe(false);
    expect(within(activity).getByText(/2 recorded actions/)).toBeTruthy();
    expect(within(activity).getByText(/1 refused \/ failed/)).toBeTruthy();
    fireEvent.click(activity.querySelector("summary"));
    expect(activity.open).toBe(true);
    expect(within(activity).getByText("read_file")).toBeTruthy();
    expect(within(transcript).getByText("write outside workspace")).toBeTruthy();
  });

  it("does not turn an agent's finished claim into a verified verdict without checks", async () => {
    detailFixture = { ...detail, verification_state: { state: "unverified",
      source: "no acceptance check was recorded" }, verification_label: "unverified",
      criteria: [], messages: [detail.messages[0], detail.messages.at(-1)] };
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Write report.md from notes.txt/ }));
    expect(await screen.findByText("Run summary")).toBeTruthy();
    const checks = screen.getByLabelText("Acceptance checks");
    expect(within(checks).getByText("unverified")).toBeTruthy();
    expect(within(checks).getByText(/No acceptance criteria were supplied/)).toBeTruthy();
    expect(screen.getByText("agent claim")).toBeTruthy();
  });

  it("uploads an attachment into the runtime workspace and names the accepted path in the task", async () => {
    mount();
    const source = new File(["real bytes"], "original.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("Choose files to attach"),
      { target: { files: [source] } });
    expect(await screen.findByText("accepted-notes.txt")).toBeTruthy();
    const placed = calls.find(call => call.options.method === "POST" &&
      call.url === "/studio/api/workspace/file?root=workspace");
    expect(placed.options.headers["X-Livium-Studio"]).toBe("1");
    expect(placed.options.headers["X-Livium-Path"]).toBe("original.txt");
    expect(placed.options.headers["X-Livium-Directory"]).toBe("");
    expect(placed.options.body).toBe(source);
    fireEvent.change(screen.getByRole("textbox", { name: "Task objective" }),
      { target: { value: "Review my attachment" } });
    fireEvent.click(screen.getByRole("button", { name: "Start the task" }));
    await waitFor(() => expect(calls.some(call => call.url === "/studio/api/chat" &&
      call.options.method === "POST")).toBe(true));
    const post = calls.find(call => call.url === "/studio/api/chat" && call.options.method === "POST");
    expect(JSON.parse(post.options.body).objective).toContain('"accepted-notes.txt"');
    expect(JSON.parse(post.options.body).objective).not.toContain('"original.txt"');
  });

  it("encodes Unicode attachment names for HTTP and shows the server-accepted name", async () => {
    uploadFixture = { path: "résumé plan.txt", name: "résumé plan.txt", root: "workspace" };
    mount();
    fireEvent.change(screen.getByLabelText("Choose files to attach"),
      { target: { files: [new File(["example"], "résumé plan.txt")] } });
    expect(await screen.findByText("résumé plan.txt")).toBeTruthy();
    const placed = calls.find(call => call.options.method === "POST" &&
      call.url === "/studio/api/workspace/file?root=workspace");
    expect(placed.options.headers["X-Livium-Path"]).toBe(encodeURIComponent("résumé plan.txt"));
  });

  it("keeps the user's prompt and attachments when a run cannot start", async () => {
    chatError = "provider probe failed";
    mount();
    fireEvent.change(screen.getByLabelText("Choose files to attach"),
      { target: { files: [new File(["content"], "original.txt")] } });
    expect(await screen.findByText("accepted-notes.txt")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Task objective" }),
      { target: { value: "Please analyse this" } });
    fireEvent.click(screen.getByRole("button", { name: "Start the task" }));
    expect(await screen.findByText(/provider probe failed/)).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Task objective" }).value).toBe("Please analyse this");
    expect(screen.getByText("accepted-notes.txt")).toBeTruthy();
  });
});
