// Livium viewer. Plain ES module, no build step, no framework.
//
// Every panel renders a response from /api/*, which the server fills from the
// database or from a live probe. Nothing is hardcoded here, so if the backend
// breaks the page goes blank rather than lying.

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { running: false };

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText} for ${url}`);
  return response.json();
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({ error: "response was not JSON" }));
  if (!response.ok) throw new Error(payload.error || `${response.status}`);
  return payload;
}

function badge(text, kind = "") {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

function kv(pairs) {
  return `<dl class="kv">${pairs
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(
      Array.isArray(v) ? v.join(", ") || "(none)" : v)}</dd>`)
    .join("")}</dl>`;
}

// A missing field used to render as nothing at all, which made a broken contract
// look like a quiet dashboard. Any panel that expects fields says so out loud.
function driftNote(label, source, names) {
  const missing = names.filter((name) => source == null || source[name] === undefined ||
    source[name] === null);
  return missing.length
    ? `<p class="hint">${esc(label)}: ${esc(missing.join(", "))} missing from the API response</p>`
    : "";
}

function truncateList(items, limit = 6) {
  if (!items?.length) return "(none)";
  const shown = items.slice(0, limit).join(", ");
  return items.length > limit ? `${shown} … +${items.length - limit} more` : shown;
}

// ------------------------------------------------------------------ panels
async function refreshStatus() {
  const [data, perms] = await Promise.all([
    getJSON("/api/status"),
    getJSON("/api/permissions"),
  ]);
  const { startup, status, isolation, provider } = data;
  const guardian = status.guardian;

  const providerBadge = provider.ok
    ? badge(`${provider.model} verified`, "ok")
    : badge("provider unverified", "bad");
  const integrityBadge = guardian.integrity.ok
    ? badge("guardian intact", "ok")
    : badge("guardian not established", "warn");
  const isolationBadge = isolation.network_isolated
    ? badge(`network isolated: ${isolation.mechanism}`, "ok")
    : badge("no network isolation on this host", "warn");
  const testDoubleBadge = provider.is_test_double ? badge("scripted test double", "bad") : "";
  $("badges").innerHTML =
    providerBadge + integrityBadge + isolationBadge + testDoubleBadge +
    badge(`${status.stats.memory} memory rows`) +
    badge(`${status.stats.event} journal events`);

  // Guardian
  const consequences = (status.guardian.consequences || [])
    .map((c) => `<li><strong>${esc(c.observation)}</strong> — ${esc(c.response)}
      ${c.enforced ? '<span class="tag bad">enforced</span>' : '<span class="tag">advisory</span>'}
      ${c.evidence ? `<div class="meta">${esc(c.evidence)}</div>` : ""}</li>`)
    .join("");
  $("guardian").innerHTML =
    kv([
      ["integrity", guardian.integrity.detail],
      ["frozen", guardian.freeze.frozen ? guardian.freeze.reason : "no"],
      ["protected files", guardian.integrity.checked.length],
      ["baseline recorded", startup.guardian_integrity.checked.length],
    ]) +
    `<p class="hint">Consequences derived from the journal:</p>` +
    `<ul class="hint">${consequences || "<li>(none outstanding)</li>"}</ul>` +
    driftNote("guardian", guardian, ["integrity", "freeze", "consequences"]) +
    driftNote("startup", startup, ["guardian_integrity", "context", "tools"]);

  // Provider + context
  $("provider").innerHTML = kv([
    ["provider", status.config.model.provider],
    ["model", status.config.model.model],
    ["base url", status.config.model.base_url],
    ["credential present", status.config.model.api_key_present ? "yes" : "no"],
    ["probe", provider.detail],
    ["context window", provider.context_window ?? "unknown"],
    ["window source", provider.context_window_source],
    ["prompt budget", startup.context.available_for_prompt],
    ["output reserve", startup.context.reserve],
    ["estimate calibration", startup.context.estimate_calibration],
  ]);

  // Isolation
  $("isolation").innerHTML =
    kv([["mechanism", isolation.mechanism],
        ["network isolated", isolation.network_isolated ? "yes" : "no"],
        ["filesystem confined", isolation.filesystem_confined ? "yes" : "no"]]) +
    `<p class="hint">${esc(isolation.detail)}</p>`;

  // Permissions come from /api/permissions (the engine's own description), not from
  // /api/status: reading them from the wrong payload left this panel empty.
  $("permissions").innerHTML = kv([
    ["read roots", perms.read_roots],
    ["write roots", perms.write_roots],
    ["network allowlist", perms.network_allowlist.length ? perms.network_allowlist : "(empty — no host reachable)"],
    ["denied binaries", truncateList(perms.denied_binaries, 14)],
    ["git refused", truncateList(perms.git_refused, 10)],
  ]) + driftNote("permissions", perms,
    ["read_roots", "write_roots", "network_allowlist", "denied_binaries", "allowed_binaries",
     "git_refused"]);
}

async function refreshCapabilities() {
  const data = await getJSON("/api/capabilities");
  const items = data.capabilities || [];
  if (!items.length) {
    $("capabilities").innerHTML =
      `<p class="empty">No capability has been proposed yet. Nothing is registered that does not
       exist, so an empty list here is the honest state.</p>`;
    return;
  }
  const rows = items.map((c) => `
    <tr>
      <td class="mono">${esc(c.name)}</td>
      <td><span class="tag ${c.status === "active" || c.status === "maintained" ? "ok"
        : c.status === "validated" ? "warn" : c.status === "rollback" || c.status === "rejected" ? "bad" : ""}">
        ${esc(c.status)}</span></td>
      <td>${esc(c.requirement)}</td>
      <td class="mono">${esc(c.lines ?? "—")}</td>
      <td class="mono">${esc(c.verification || "—")}</td>
      <td class="mono">${esc(c.promoted_by || "—")}</td>
    </tr>`).join("");
  const loaded = (data.loaded_capability_tools || []).length
    ? `<p class="hint">promoted tools currently callable: ${esc(data.loaded_capability_tools.join(", "))}</p>`
    : `<p class="hint">no promoted capability tools are loaded</p>`;
  const failures = (data.load_failures || []).length
    ? `<p class="hint">load failures: ${esc(data.load_failures.join(" | "))}</p>` : "";
  $("capabilities").innerHTML =
    `<table><thead><tr><th>name</th><th>status</th><th>requirement</th><th>lines</th>
     <th>verification</th><th>promoted by</th></tr></thead><tbody>${rows}</tbody></table>` +
    loaded + failures;
}

async function refreshMemory(query = "") {
  const url = query ? `/api/memory?q=${encodeURIComponent(query)}&limit=25` : "/api/memory?limit=15";
  const data = await getJSON(url);
  const records = data.records || [];
  const dupes = (data.duplicates || []).length
    ? `<p class="hint">${data.duplicates.length} duplicate group(s) detected — identical normalised
       text. Consolidation is an explicit decision: <code>livium memory consolidate</code>.</p>` : "";
  $("memory").innerHTML = dupes + (records.length ? `<div class="list">${records.map((r) => `
      <div class="item">
        <div class="meta">
          <span class="tag">mem:${r.id}</span><span class="tag">${esc(r.kind)}</span>
          <span class="tag">${esc(r.verification)}</span>
          <span>${esc(r.confidence_band)} trust (${r.confidence})</span>
          ${r.protected ? '<span class="tag bad">protected</span>' : ""}
          <span>${esc(r.updated_at)}</span>
          ${r.match ? `<span>${esc(r.match)}</span>` : ""}
        </div>
        <div class="body">${esc(r.content)}</div>
        ${r.detail ? `<div class="meta">${esc(r.detail).slice(0, 400)}</div>` : ""}
        ${r.evidence ? `<div class="meta">evidence: ${esc(r.evidence).slice(0, 240)}</div>` : ""}
      </div>`).join("")}</div>`
    : `<p class="empty">No memory records match. Storing nothing on a topic is a real answer, not
       an error.</p>`);
}

async function refreshEvents() {
  const type = $("event-filter").value;
  const data = await getJSON(`/api/events?limit=45${type ? `&type=${type}` : ""}`);
  const events = data.events || [];
  if (!events.length) {
    $("events").innerHTML = `<p class="empty">journal is empty</p>`;
    return;
  }
  $("events").innerHTML = `<table><thead><tr><th>when</th><th>type</th><th>task</th>
    <th>payload</th></tr></thead><tbody>${events.map((e) => {
      const bad = /violation|freeze|failed|error|loop_detected/.test(e.type);
      return `<tr><td class="mono">${esc(e.ts)}</td>
        <td><span class="tag ${bad ? "bad" : ""}">${esc(e.type)}</span></td>
        <td class="mono">${esc((e.task_id || "").slice(0, 8) || "—")}</td>
        <td class="mono">${esc(JSON.stringify(e.payload).slice(0, 260))}</td></tr>`;
    }).join("")}</tbody></table>`;
}

async function refreshTasks() {
  const data = await getJSON("/api/tasks?limit=12");
  const tasks = data.tasks || [];
  if (!tasks.length) {
    $("tasks").innerHTML = `<p class="empty">no task has run on this database yet</p>`;
    return;
  }
  $("tasks").innerHTML = `<table><thead><tr><th>id</th><th>status</th><th>objective</th>
    <th>steps</th><th>updated</th></tr></thead><tbody>${tasks.map((t) => `
      <tr><td class="mono">${esc(t.id.slice(0, 8))}</td>
        <td><span class="tag ${t.status === "complete" ? "ok" : t.status === "active" ? "warn" : "bad"}">
          ${esc(t.status)}</span></td>
        <td>${esc(t.objective).slice(0, 120)}</td>
        <td class="mono">${esc(t.steps)}</td>
        <td class="mono">${esc(t.updated_at)}</td></tr>`).join("")}</tbody></table>`;
}

async function refreshContext() {
  const data = await getJSON("/api/context?limit=12");
  const reports = data.reports || [];
  if (!reports.length) {
    $("context").innerHTML = `<p class="empty">no context has been constructed yet</p>`;
    return;
  }
  $("context").innerHTML = `<table><thead><tr><th>task</th><th>step</th><th>est. tokens</th>
    <th>prompt chars</th><th>budget</th><th>omitted</th></tr></thead><tbody>${reports.map((r) => {
      const budget = JSON.parse(r.budget || "{}");
      const omitted = JSON.parse(r.omitted || "{}");
      return `<tr><td class="mono">${esc(r.task_id.slice(0, 8))}</td>
        <td class="mono">${esc(r.step)}</td>
        <td class="mono">${esc(r.estimated)}</td>
        <td class="mono">${esc(r.prompt_chars)}</td>
        <td class="mono">${esc(budget.window ?? "?")} window / ${esc(budget.reserve ?? "?")} reserve</td>
        <td class="mono">${esc(Object.keys(omitted).length ? JSON.stringify(omitted).slice(0, 200) : "nothing")}</td></tr>`;
    }).join("")}</tbody></table>`;
}

async function refreshAll() {
  try {
    await Promise.all([refreshStatus(), refreshCapabilities(), refreshMemory($("memory-query").value),
                       refreshEvents(), refreshTasks(), refreshContext()]);
  } catch (error) {
    console.error(error);
    $("badges").innerHTML = badge(`viewer error: ${error.message}`, "bad");
  }
}

// ------------------------------------------------------------------ actions
let pollTimer = null;

async function runTask() {
  const objective = $("objective").value.trim();
  if (!objective) return;
  let criteria = [];
  const raw = $("criteria").value.trim();
  if (raw) {
    try {
      criteria = JSON.parse(raw);
    } catch (error) {
      $("run-output").classList.remove("hidden");
      $("run-output").textContent = `acceptance criteria are not valid JSON: ${error.message}`;
      return;
    }
  }
  state.running = true;
  $("run").disabled = true;
  $("run-output").classList.remove("hidden");
  $("run-output").innerHTML = `<span class="spinner"></span> running… the model is being called for
    real; this page is polling the journal meanwhile.`;
  pollTimer = setInterval(() => refreshEvents().catch(() => {}), 2500);
  try {
    const result = await postJSON("/api/run", { objective, criteria });
    $("run-output").textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    $("run-output").textContent = `run failed: ${error.message}`;
  } finally {
    clearInterval(pollTimer);
    state.running = false;
    $("run").disabled = false;
    refreshAll();
  }
}

function wire() {
  $("refresh").addEventListener("click", refreshAll);
  $("run").addEventListener("click", runTask);
  $("event-filter").addEventListener("change", () => refreshEvents().catch(() => {}));
  $("memory-search").addEventListener("click", async () => {
    const query = $("memory-query").value.trim();
    await refreshMemory(query);
    if ($("memory-remember").checked && query) {
      try {
        await postJSON("/api/memory", {
          kind: "experience", content: query, confidence: 0.6,
          verification: "claimed", tags: ["viewer"],
        });
        await refreshMemory(query);
      } catch (error) {
        console.error(error);
      }
    }
  });
  $("memory-query").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("memory-search").click();
  });
}

wire();
refreshAll();
setInterval(() => { if (!state.running) refreshAll(); }, 15000);
