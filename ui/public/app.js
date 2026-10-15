// Livium Studio — the browser side.
//
// Plain ES module, no build step, no framework. Every value on the screen comes from
// /studio/api/* or /api/* (the same Python runtime the CLI drives), and every string
// that came from the runtime is escaped before it is rendered: model output, file
// names and file contents are data, not markup.

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const WRITE_HEADER = { "X-Livium-Studio": "1" };

const ui = {
  studio: null,
  tasks: [],
  current: null,
  run: null,
  root: "workspace",
  directory: "",
  selected: null,
  settingsDirty: false,
  activeTab: "chat",
};

async function api(path, { method = "GET", body = null, headers = {} } = {}) {
  const options = { method, headers: { Accept: "application/json", ...headers } };
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let payload;
  try { payload = text ? JSON.parse(text) : {}; } catch { payload = { error: text.slice(0, 400) }; }
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

const write = (path, body) => api(path, { method: "POST", body, headers: WRITE_HEADER });

function alert(message) {
  const node = $("alert");
  if (!message) return node.classList.add("hidden");
  node.textContent = message;
  node.classList.remove("hidden");
}

function bytes(size) {
  if (size === null || size === undefined) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} kB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}

const when = (iso) => iso ? new Date(iso).toLocaleTimeString() : "";

function kb(pairs) {
  return `<dl class="kv">${pairs.filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}

// ------------------------------------------------------------------- tabs
function showTab(name) {
  ui.activeTab = name;
  for (const button of $("tabs").children) button.classList.toggle("active", button.dataset.tab === name);
  for (const panel of document.querySelectorAll(".panel")) {
    panel.classList.toggle("hidden", panel.id !== `panel-${name}`);
  }
  if (name === "workspace") loadWorkspace().catch((error) => alert(error.message));
  if (name === "journal") refreshJournal().catch((error) => alert(error.message));
  if (name === "settings") refreshBackendLog().catch(() => {});
}

// ------------------------------------------------------------------ state
async function refreshState({ probe = false } = {}) {
  // `probe` asks the runtime for a fresh provider probe. The ordinary poll does not: a
  // probe is a real request to the provider and a real journal row, so the page reuses
  // the cached one and shows its age instead.
  const payload = await api(`/studio/api/state${probe ? "?probe=1" : ""}`);
  ui.studio = payload;
  const provider = payload.provider ?? {};
  const badges = [];
  const warn = (payload.settings.warnings ?? []).length > 0;
  if (provider.model) {
    const probed = provider.fresh ? "just now" : `${Math.round(provider.age_seconds ?? 0)}s ago`;
    badges.push(`<span class="badge ${provider.ok ? "ok" : "bad"}">provider ${provider.ok ? "reachable" : "not usable"} · probed ${esc(probed)}</span>`);
  } else {
    badges.push(`<span class="badge warn">provider unknown</span>`);
  }
  badges.push(`<span class="badge accent">${esc(payload.settings.provider)}${payload.settings.model ? `:${esc(payload.settings.model)}` : ""}</span>`);
  badges.push(`<span class="badge ${payload.backend.running ? "ok" : "bad"}">runtime ${payload.backend.running ? `:${payload.backend.port}` : "down"}</span>`);
  badges.push(`<span class="badge ${payload.settings.api_key_set ? "ok" : "warn"}">key ${payload.settings.api_key_set ? esc(payload.settings.api_key_hint) : "not set"}</span>`);
  if (payload.runtime?.isolation) {
    badges.push(`<span class="badge ${payload.runtime.isolation.network_isolated ? "ok" : "warn"}">network ${payload.runtime.isolation.network_isolated ? "isolated" : "open"}</span>`);
  }
  if (warn) badges.push(`<span class="badge warn">test double configured</span>`);
  $("top-badges").innerHTML = badges.join(" ");
  $("home-path").textContent = `runtime home: ${payload.studio.home}`;

  const runtimeIssues = [];
  if (payload.backend.last_error) runtimeIssues.push(`the runtime backend failed to start: ${payload.backend.last_error}`);
  if (payload.error) runtimeIssues.push(payload.error);
  if (provider.detail && provider.ok === false) runtimeIssues.push(`provider: ${provider.detail}`);
  for (const warning of payload.settings.warnings ?? []) runtimeIssues.push(warning);
  alert(runtimeIssues.join("\n\n"));

  renderRoots(payload.studio.roots);
  renderProviderState(payload);
  if (!ui.settingsDirty) fillSettingsForm(payload.settings);
  return payload;
}

function renderProviderState(payload) {
  const provider = payload.provider ?? {};
  $("provider-state").innerHTML = kb([
    ["configured", `${payload.settings.provider}${payload.settings.model ? ` / ${payload.settings.model}` : ""}`],
    ["base url", payload.settings.base_url || "(provider default)"],
    ["api key", payload.settings.api_key_set
      ? `${payload.settings.api_key_hint} · sha256 ${payload.key_fingerprint}` : "not set"],
    ["reachable", provider.ok === undefined ? "(not probed)" : String(provider.ok)],
    ["probe", provider.age_seconds === undefined ? "(this runtime does not report it)"
      : provider.fresh ? "taken just now"
      : `cached, taken ${provider.age_seconds}s ago (re-probe to refresh)`],
    ["detail", provider.detail],
    ["context window", provider.context_window ? `${provider.context_window} (${provider.context_window_source})` : ""],
    ["test double", provider.is_test_double === true ? "yes — no real model output" : (provider.is_test_double === false ? "no" : "")],
    ["runtime events", payload.runtime?.stats?.event],
    ["isolation", payload.runtime?.isolation?.detail],
  ]) + `<p class="hint">A probe is a real request. When it fails, the detail above is the
    provider's own answer — the runtime does not fall back to a test double unless you
    configure one.</p>`;
}

// ------------------------------------------------------------------- chat
async function refreshTasks() {
  const payload = await api("/studio/api/chat?limit=60");
  ui.tasks = payload.tasks ?? [];
  $("tasks").innerHTML = ui.tasks.length ? ui.tasks.map((task) => `
    <div class="entry ${ui.current?.task_id === task.task_id ? "selected" : ""}" data-task="${esc(task.task_id)}">
      <span class="name">
        <span class="title">${esc((task.objective || "(no objective)").slice(0, 90))}</span>
        <span class="meta">${esc(task.task_id)} · ${esc(task.status)}${task.verification ? ` · ${esc(task.verification)}` : ""}
          ${task.run?.running ? " · running now" : ""}</span>
      </span>
    </div>`).join("") : `<p class="empty">No task has run in this home yet.</p>`;
  for (const entry of $("tasks").querySelectorAll("[data-task]")) {
    entry.addEventListener("click", () => selectTask(entry.dataset.task).catch((error) => alert(error.message)));
  }
  if (!ui.current && ui.tasks.length) await selectTask(ui.tasks[0].task_id);
}

async function selectTask(taskId) {
  const payload = await api(`/studio/api/chat/${encodeURIComponent(taskId)}`);
  ui.current = payload;
  ui.run = payload.run ?? ui.run;
  $("chat-title").textContent = payload.task?.objective?.slice(0, 120) || taskId;
  $("chat-sub").textContent = `${taskId} · ${payload.task?.status ?? ""} · ` +
    `${payload.counts.messages} message(s), ${payload.counts.artifacts} file(s) written, ` +
    `${payload.counts.refusals} refusal(s)`;
  renderChecks(payload);
  renderTranscript(payload);
  renderArtifacts(payload);
  renderSteps(payload);
  renderRunControls();
  for (const entry of $("tasks").querySelectorAll("[data-task]")) {
    entry.classList.toggle("selected", entry.dataset.task === taskId);
  }
  return payload;
}

/**
 * The acceptance checks above the transcript. Every word here comes from the runtime:
 * the criteria as written, and the outcome it recorded for each one. A task with no
 * criteria says so, because "the agent said it was done" is not a verification.
 */
function renderChecks(payload) {
  const state = payload.verification_state ?? null;
  const criteria = payload.criteria ?? [];
  const rows = [];
  if (state) {
    const tone = state.state === "verified" ? "ok"
      : (state.state === "not verified" ? "bad" : (state.state === "not run" ? "" : "warn"));
    rows.push(`<div class="line"><span class="badge ${tone}">${esc(payload.verification_label ?? state.state)}</span>
      <span class="why">${esc(state.source ?? "")}</span></div>`);
  }
  if (!criteria.length) {
    rows.push(`<div class="line"><span class="why">This task has no acceptance criteria, so nothing
      about it was verified — the summary below is the agent's own account.</span></div>`);
  } else {
    for (const entry of criteria) {
      const tone = entry.outcome === "passed" ? "ok"
        : (entry.outcome === "failed" || entry.outcome === "error" ? "bad" : "warn");
      rows.push(`<div class="line">
        <span class="badge ${tone}">${esc(entry.outcome ?? "no outcome recorded")}</span>
        <span class="spec">${esc(JSON.stringify(entry.spec))}</span>
        ${entry.detail ? `<span class="why">${esc(entry.detail)}</span>` : ""}</div>`);
    }
  }
  $("chat-verification").innerHTML = rows.join("");
}

function renderTranscript(payload) {
  if (!payload.messages.length) {
    $("transcript").innerHTML = `<p class="empty">This task has no journalled messages.</p>`;
    return;
  }
  $("transcript").innerHTML = payload.messages.map((message) => {
    const meta = Object.entries(message.meta ?? {})
      .filter(([, value]) => value !== undefined && value !== null && value !== false && value !== "")
      .map(([key, value]) => `${esc(key)}=${esc(Array.isArray(value) ? value.join(",") : value)}`)
      .join(" ");
    return `<div class="message ${esc(message.role)}">
      <div class="head"><span class="who">${esc(message.title || message.role)}</span>
        <span class="when">${esc(when(message.at))}</span></div>
      ${message.body ? `<div class="body">${esc(message.body)}</div>` : ""}
      ${meta ? `<div class="meta">${meta}</div>` : ""}
    </div>`;
  }).join("");
  $("transcript").scrollTop = $("transcript").scrollHeight;
}

function renderArtifacts(payload) {
  const artifacts = payload.artifacts ?? [];
  $("artifacts").innerHTML = artifacts.length ? `<table><thead><tr><th>file</th><th>bytes</th>
    <th>sha256</th><th></th></tr></thead><tbody>${artifacts.map((artifact) => `
      <tr><td class="mono">${esc(artifact.path)}${artifact.in_workspace ? "" : " (outside the workspace)"}</td>
      <td class="mono">${esc(bytes(artifact.bytes))}</td>
      <td class="mono">${esc(String(artifact.sha256 ?? "").slice(0, 12))}…</td>
      <td>${artifact.in_workspace ? `<a class="link" href="/studio/api/workspace/file?root=workspace&path=${encodeURIComponent(artifact.path)}&download=1">download</a>` : ""}</td>
      </tr>`).join("")}</tbody></table>`
    : `<p class="empty">Nothing written by the selected task.</p>`;
}

function renderSteps(payload) {
  const steps = payload.steps ?? [];
  if (!steps.length) {
    $("steps").innerHTML = `<p class="empty">No steps recorded for this task.</p>`;
    return;
  }
  $("steps").innerHTML = `<table><thead><tr><th>#</th><th>phase</th><th>status</th>
    <th>detail</th></tr></thead><tbody>${steps.map((step) => `<tr>
      <td class="mono">${esc(step.idx)}</td><td>${esc(step.phase)}</td>
      <td>${esc(step.status)}</td><td>${esc(String(step.detail ?? "").slice(0, 220))}</td>
    </tr>`).join("")}</tbody></table>`;
}

function renderRunControls() {
  const run = ui.run;
  const controls = [];
  if (run) {
    controls.push(`<span class="badge ${run.running ? "accent" : run.status === "exited" ? "ok" : "bad"}">run ${esc(run.status)}</span>`);
    if (run.running) controls.push(`<button class="ghost danger" id="stop-run">stop this task</button>`);
  }
  $("run-controls").innerHTML = controls.join(" ");
  const stop = $("stop-run");
  if (stop) {
    stop.addEventListener("click", async () => {
      await write(`/studio/api/runs/${encodeURIComponent(run.id)}/stop`);
      alert("stopped the run process; the runtime's recovery machinery can inspect the " +
            "interrupted task on the next run.");
      await refresh();
    });
  }
}

function renderRunLive() {
  const run = ui.run;
  if (!run) {
    $("run-live").innerHTML = `<p class="empty">No run yet.</p>`;
    $("run-log").textContent = "(empty)";
    return;
  }
  $("run-live").innerHTML = kb([
    ["task id", run.task_id], ["run id", run.id], ["process", run.pid],
    ["status", `${run.status}${run.exit_code === null ? "" : ` (exit ${run.exit_code})`}`],
    ["started", run.started_at], ["finished", run.finished_at],
  ]);
  $("run-log").textContent = run.log_tail || "(no output yet)";
}

async function sendTask(event) {
  event.preventDefault();
  const objective = $("objective").value.trim();
  if (!objective) return alert("Write what you want the agent to do first.");
  let criteria = null;
  const rawCriteria = $("criteria").value.trim();
  if (rawCriteria) {
    try {
      criteria = JSON.parse(rawCriteria);
    } catch (error) {
      return alert(`the acceptance criteria are not JSON: ${error.message}`);
    }
  }
  const body = { objective, criteria, force: $("force").checked };
  if ($("resume").checked && ui.current) {
    body.task_id = ui.current.task_id;
    body.resume = true;
  }
  try {
    const payload = await write("/studio/api/chat", body);
    $("objective").value = "";
    alert(null);
    ui.run = payload.run;
    await refreshTasks();
    await selectTask(payload.run.task_id);
    showTab("chat");
    poll(true);
  } catch (error) {
    alert(`the task could not be started: ${error.message}`);
  }
}

// --------------------------------------------------------------- workbench
async function refreshRun() {
  if (!ui.run) return;
  try {
    const payload = await api(`/studio/api/runs/${encodeURIComponent(ui.run.id)}`);
    ui.run = payload.run;
  } catch {
    return;
  }
  renderRunLive();
  renderRunControls();
  if (!ui.run.running) ui.run = ui.run; // keep the finished run on screen for its log
}

// --------------------------------------------------------------- workspace
function renderRoots(roots) {
  const select = $("root-select");
  const wanted = roots ?? {};
  const names = Object.keys(wanted);
  if (select.dataset.names === names.join(",")) return;
  select.dataset.names = names.join(",");
  select.innerHTML = names.map((name) =>
    `<option value="${esc(name)}">${esc(wanted[name].label)} — ${esc(wanted[name].path)}</option>`).join("");
  select.value = ui.root;
}

async function loadWorkspace() {
  const payload = await api(`/studio/api/workspace?root=${encodeURIComponent(ui.root)}&path=${encodeURIComponent(ui.directory)}`);
  ui.listing = payload;
  $("crumbs").textContent = `${payload.root.path.replace(/\/$/, "")}/${payload.path}`;
  $("file-list").innerHTML = payload.entries.length ? payload.entries.map((entry) => `
    <div class="entry" data-path="${esc(entry.path)}" data-kind="${esc(entry.kind)}">
      <span class="name">${entry.kind === "directory" ? "📁" : entry.kind === "symlink" ? "🔗" : "📄"}
        ${esc(entry.name)}</span>
      <span class="size">${entry.kind === "directory" ? "" : esc(bytes(entry.size))}</span>
    </div>`).join("") : `<p class="empty">This folder is empty.</p>`;
  for (const entry of $("file-list").querySelectorAll("[data-path]")) {
    entry.addEventListener("click", () => {
      if (entry.dataset.kind === "directory") {
        ui.directory = entry.dataset.path;
        ui.selected = null;
        loadWorkspace().catch((error) => alert(error.message));
      } else {
        openFile(entry.dataset.path).catch((error) => alert(error.message));
      }
    });
  }
  const recent = await api(`/studio/api/workspace/recent?root=${encodeURIComponent(ui.root)}&minutes=180`);
  $("recent").innerHTML = recent.files.length
    ? `changed in the last 3 hours: ` + recent.files.slice(0, 8)
        .map((file) => `<a class="link" data-open="${esc(file.path)}" href="#">${esc(file.path)}</a>`).join(" · ")
    : "nothing changed in this root in the last 3 hours";
  for (const link of $("recent").querySelectorAll("[data-open]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      openFile(link.dataset.open).catch((error) => alert(error.message));
    });
  }
}

const IMAGE_TYPES = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]);

async function openFile(relative) {
  const payload = await api(`/studio/api/workspace/file?root=${encodeURIComponent(ui.root)}&path=${encodeURIComponent(relative)}`);
  ui.selected = payload;
  $("file-title").textContent = payload.name;
  $("file-sub").innerHTML = `${esc(bytes(payload.size))} · ${esc(payload.modified)} · ` +
    `sha256 <span class="mono">${esc(payload.sha256.slice(0, 16))}…</span> · ` +
    `<a class="link" href="/studio/api/workspace/file?root=${encodeURIComponent(ui.root)}&path=${encodeURIComponent(relative)}&download=1">download</a>`;
  const extension = (payload.name.split(".").pop() ?? "").toLowerCase();
  if (payload.kind === "text") {
    $("file-preview").textContent = payload.content + (payload.truncated ? "\n… (truncated for preview; download for the whole file)" : "");
  } else if (IMAGE_TYPES.has(extension)) {
    $("file-preview").innerHTML = `<img alt="${esc(payload.name)}" style="max-width:100%" ` +
      `src="/studio/api/workspace/file?root=${encodeURIComponent(ui.root)}&path=${encodeURIComponent(relative)}&download=1">`;
  } else {
    $("file-preview").textContent = `${payload.name} is binary (${bytes(payload.size)}); ` +
      `use download to get it. The Studio does not try to render files it cannot read as text.`;
  }
}

async function upload(files) {
  for (const file of files) {
    const response = await fetch(
      `/studio/api/workspace/file?root=${encodeURIComponent(ui.root)}`,
      { method: "POST", headers: { ...WRITE_HEADER, "X-Livium-Path": file.name,
                                   "X-Livium-Directory": ui.directory,
                                   "Content-Type": "application/octet-stream" },
        body: file });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || response.statusText);
  }
  await loadWorkspace();
}

async function refreshJournal() {
  const type = $("event-type").value;
  const payload = await api(`/api/events?limit=120${type ? `&type=${encodeURIComponent(type)}` : ""}`);
  const events = payload.events ?? [];
  $("event-count").textContent = `${events.length} event(s) · newest first`;
  $("events").innerHTML = events.map((event) => `
    <div class="event"><span class="type">${esc(event.type)}</span>
      <span class="hint-inline">${esc(event.ts)} ${event.task_id ? `· ${esc(event.task_id)}` : ""}</span>
      <div class="json">${esc(JSON.stringify(event.payload).slice(0, 400))}</div></div>`).join("")
    || `<p class="empty">No events.</p>`;
  if ($("event-type").options.length <= 1) {
    const types = [...new Set(events.map((event) => event.type))].sort();
    $("event-type").innerHTML = `<option value="">all types</option>` +
      types.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
  }

  const [capabilities, discovery, integrity, ledger] = await Promise.all([
    api("/api/capabilities"), api("/api/discovery?limit=6"), api("/api/integrity"), api("/api/ledger?limit=8"),
  ]);
  $("capabilities").innerHTML = capabilities.capabilities.length
    ? `<table><thead><tr><th>name</th><th>status</th><th>tools</th></tr></thead><tbody>` +
      capabilities.capabilities.map((capability) => `<tr><td>${esc(capability.name)}</td>
        <td>${esc(capability.status)}</td><td class="mono">${esc((capability.tools ?? []).map((tool) => tool.name ?? tool).join(", "))}</td></tr>`).join("") +
      `</tbody></table>`
    : `<p class="empty">No capability has been proposed yet.</p>`;
  $("discovery").innerHTML = kb([
    ["queue", Object.entries(discovery.counts).map(([k, v]) => `${k} ${v}`).join(", ")],
    ["open items", (discovery.records ?? []).slice(0, 4).map((record) => record.title).join(" · ")],
    ["cycles", (discovery.cycles ?? []).length],
    ["stopped", (discovery.stopped_cycles ?? []).length],
  ]) + `<p class="hint">${esc(discovery.note)}</p>`;
  $("integrity").innerHTML = kb([
    ["watched", (integrity.components ?? []).length],
    ["status", integrity.inspection?.status],
    ["detections", integrity.enforcement?.detections],
    ["recoveries", integrity.enforcement?.recoveries],
    ["unresolved", (integrity.enforcement?.unresolved_detections ?? []).length],
    ["frozen", integrity.frozen?.frozen ? "yes" : "no"],
  ]);
  $("ledger").innerHTML = ledger.rows.length
    ? `<table><thead><tr><th>task</th><th>status</th><th>verified</th><th>model calls</th>
       <th>actual tokens</th><th>estimate</th></tr></thead><tbody>` + ledger.rows.map((row) => `
       <tr><td class="mono">${esc(String(row.task_id).slice(0, 8))}</td><td>${esc(row.status)}</td>
       <td>${esc(row.verification_state ?? "—")}</td><td class="mono">${esc(row.model_calls)}</td>
       <td class="mono">${row.prompt_tokens_actual === null && row.completion_tokens_actual === null
         ? "not reported" : `${esc(row.prompt_tokens_actual ?? 0)} + ${esc(row.completion_tokens_actual ?? 0)}`}</td>
       <td class="mono">${esc(row.tokens_estimated ?? "—")}</td></tr>`).join("") + `</tbody></table>`
    : `<p class="empty">No task has been measured yet.</p>`;
}

// ---------------------------------------------------------------- settings
function fillSettingsForm(settings) {
  $("provider").value = settings.provider;
  $("model").value = settings.model;
  $("base-url").value = settings.base_url;
  $("extra-env").value = Object.entries(settings.extra ?? {}).map(([key, value]) => `${key}=${value}`).join("\n");
  $("settings-path").textContent = `${ui.studio?.studio.home ?? ""}/settings.json`;
  const key = $("api-key");
  key.value = "";
  key.placeholder = settings.api_key_set
    ? `a key is set (${settings.api_key_hint}); paste a new one to replace it`
    : "paste the key; it is never echoed back";
}

function extraFromForm() {
  const extra = {};
  for (const line of $("extra-env").value.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index < 0) throw new Error(`"${trimmed}" is not NAME=value`);
    extra[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim();
  }
  return extra;
}

async function saveSettings({ restart, probe }) {
  try {
    const body = {
      provider: $("provider").value,
      model: $("model").value.trim(),
      base_url: $("base-url").value.trim(),
      extra: extraFromForm(),
      restart,
    };
    const typed = $("api-key").value;
    if (typed) body.api_key = typed;
    const payload = await write("/studio/api/settings", body);
    $("api-key").value = "";
    ui.settingsDirty = false;
    $("settings-note").innerHTML = `saved at ${esc(payload.settings.updated_at)} · ` +
      `key ${payload.settings.api_key_set ? esc(payload.settings.api_key_hint) : "not set"} · ` +
      `sha256 <span class="mono">${esc(payload.key_fingerprint ?? "—")}</span> · ${esc(payload.note)}`;
    await refreshState();
    if (probe !== false) {
      const provider = payload.provider ?? ui.studio?.provider ?? {};
      $("settings-note").innerHTML += provider.ok
        ? `<br>the provider answered: ${esc(provider.detail ?? "ok")} · context window ${esc(provider.context_window ?? "unknown")}`
        : `<br><span class="badge bad">the provider did not answer</span> ${esc(provider.detail ?? "")}`;
    }
  } catch (error) {
    $("settings-note").innerHTML = `<span class="badge bad">not saved</span> ${esc(error.message)}`;
  }
}

async function refreshBackendLog() {
  const payload = await api("/studio/api/backend/log");
  $("backend-log").textContent = payload.log || "(the runtime has logged nothing yet)";
}

// ---------------------------------------------------------------- polling
let timer = null;
let fast = null;

async function refresh() {
  try {
    await refreshState();
    if (ui.activeTab === "chat") {
      await refreshTasks();
      if (ui.current) await selectTask(ui.current.task_id);
    }
  } catch (error) {
    alert(error.message);
  }
}

async function pollFast() {
  if (!ui.run?.running) return;
  try {
    await refreshRun();
    if (ui.current) await selectTask(ui.current.task_id);
    if (ui.activeTab === "workbench") renderRunLive();
    if (!ui.run?.running) {
      await refreshTasks();
      if (ui.current) await selectTask(ui.current.task_id);
    }
  } catch (error) {
    console.error(error);
  }
}

function poll(startFast = false) {
  if (!timer) timer = setInterval(() => refresh().catch(() => {}), 8000);
  if (startFast && !fast) fast = setInterval(() => pollFast().catch(() => {}), 1500);
  if (startFast) pollFast();
}

function wire() {
  for (const button of $("tabs").children) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }
  $("composer").addEventListener("submit", sendTask);
  $("reload-tasks").addEventListener("click", () => refreshTasks().catch((error) => alert(error.message)));
  $("reload-events").addEventListener("click", () => refreshJournal().catch((error) => alert(error.message)));
  $("event-type").addEventListener("change", () => refreshJournal().catch((error) => alert(error.message)));

  $("root-select").addEventListener("change", () => {
    ui.root = $("root-select").value;
    ui.directory = "";
    ui.selected = null;
    loadWorkspace().catch((error) => alert(error.message));
  });
  $("new-folder").addEventListener("click", async () => {
    const name = prompt("Folder name");
    if (!name) return;
    try {
      await write("/studio/api/workspace/dir", { path: [ui.directory, name].filter(Boolean).join("/") });
      await loadWorkspace();
    } catch (error) { alert(error.message); }
  });
  $("delete-entry").addEventListener("click", async () => {
    if (!ui.selected) return alert("Select a file first.");
    if (!confirm(`Delete ${ui.selected.path}? This removes it from the runtime's own directory.`)) return;
    try {
      await fetch(`/studio/api/workspace/entry?root=${encodeURIComponent(ui.root)}&path=${encodeURIComponent(ui.selected.path)}`,
                  { method: "DELETE", headers: WRITE_HEADER });
      ui.selected = null;
      $("file-preview").textContent = "(nothing selected)";
      await loadWorkspace();
    } catch (error) { alert(error.message); }
  });

  const drop = $("drop");
  drop.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", async (event) => {
    event.preventDefault();
    drop.classList.remove("over");
    try { await upload([...event.dataTransfer.files]); } catch (error) { alert(error.message); }
  });
  drop.addEventListener("click", () => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.addEventListener("change", () => upload([...input.files]).catch((error) => alert(error.message)));
    input.click();
  });

  $("settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    saveSettings({ restart: true });
  });
  $("save-only").addEventListener("click", () => saveSettings({ restart: false, probe: false }));
  $("test-provider").addEventListener("click", () => saveSettings({ restart: true, probe: true }));
  $("reprobe").addEventListener("click", async () => {
    try {
      await refreshState({ probe: true });
      renderProviderState(ui.studio ?? {});
      await refreshBackendLog();
    } catch (error) { alert(error.message); }
  });
  $("restart-backend").addEventListener("click", async () => {
    try {
      await write("/studio/api/backend/restart", {});
      await refreshState();
      await refreshBackendLog();
    } catch (error) { alert(error.message); }
  });
  $("toggle-key").addEventListener("click", () => {
    const field = $("api-key");
    field.type = field.type === "password" ? "text" : "password";
    $("toggle-key").textContent = field.type === "password" ? "show" : "hide";
  });
  $("clear-key").addEventListener("click", async () => {
    if (!confirm("Remove the stored API key?")) return;
    try {
      await write("/studio/api/settings", { clear_api_key: true, restart: true });
      await refreshState();
      $("settings-note").innerHTML = `<span class="badge warn">the stored key was removed</span>`;
    } catch (error) { alert(error.message); }
  });
  for (const field of ["provider", "model", "base-url", "extra-env"]) {
    $(field).addEventListener("input", () => { ui.settingsDirty = true; });
  }
}

wire();
showTab("chat");
refresh()
  .then(() => poll(ui.tasks.some((task) => task.run?.running)))
  .catch((error) => alert(`the Studio could not read the runtime: ${error.message}`));
