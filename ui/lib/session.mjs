// How a task is shown in the Studio's chat panel.
//
// This module answers one question: given the runtime's own journal for one task, what
// happened? Every message, artifact and refusal below comes from a journalled event or
// from a task row -- there is no narration and no inference about what the agent
// "probably" did. Where the runtime recorded nothing, the transcript says so instead of
// rendering a plausible screen, because a chat view that invents its own history is
// worse than no chat view.
//
// The payload shapes are the ones the runtime writes; they were read out of a real
// database rather than guessed, and `test/session.test.mjs` pins them.

import path from "node:path";

export function artifactLocation(backendEnv, absolute) {
  if (!absolute || !path.isAbsolute(absolute)) return { root: null, relative_path: null };
  const home = backendEnv.LIVIUM_HOME || path.dirname(backendEnv.LIVIUM_WORKSPACE || "");
  const roots = {
    workspace: backendEnv.LIVIUM_WORKSPACE,
    lab: home ? path.join(home, "capability-lab") : null,
    promoted: backendEnv.LIVIUM_PROMOTED_DIR,
  };
  const target = path.resolve(absolute);
  for (const [root, raw] of Object.entries(roots)) {
    if (!raw) continue;
    const base = path.resolve(raw);
    if (target === base || target.startsWith(base + path.sep)) {
      return { root, relative_path: path.relative(base, target).split(path.sep).join("/") };
    }
  }
  return { root: null, relative_path: null };
}

export function relativeToWorkspace(backendEnv, absolute) {
  const workspace = backendEnv.LIVIUM_WORKSPACE || "";
  if (!workspace) return { path: absolute, in_workspace: false };
  const root = path.resolve(workspace);
  const target = path.resolve(absolute);
  if (target === root || target.startsWith(root + path.sep)) {
    return { path: path.relative(root, target).split(path.sep).join("/"), in_workspace: true };
  }
  return { path: target, in_workspace: false };
}

/** Turn one task's journal into the transcript the chat panel renders. */
export function transcriptOf(taskId, { task, steps, verification }, events, backendEnv) {
  const messages = [];
  const artifacts = [];
  const refusals = [];
  for (const event of events) {
    const payload = event.payload ?? {};
    switch (event.type) {
      case "task_started": {
        // The task row is what the operator asked for; the event is what the run was
        // started with. When the two disagree -- a resumed task given a new objective --
        // the difference is shown rather than silently resolved in one direction.
        const asked = String(task.objective ?? payload.objective ?? "");
        const journalled = String(payload.objective ?? "");
        messages.push({
          role: "user", at: event.ts, title: "objective", body: asked,
          meta: journalled && journalled !== asked ? { journalled_objective: journalled } : {},
        });
        break;
      }
      case "model_reply": {
        const usage = payload.usage ?? {};
        const tokens = usage.prompt_tokens ?? usage.input_tokens;
        const completion = usage.completion_tokens ?? usage.output_tokens;
        messages.push({
          role: "assistant", at: event.ts, title: payload.model ?? "model",
          body: String(payload.text ?? ""),
          meta: {
            provider: payload.provider, stop_reason: payload.stop_reason,
            latency_ms: payload.latency_ms,
            tools_requested: (payload.tool_calls ?? []).map((call) => call.name ?? call),
            // What the provider actually reported, or nothing. A scripted double reports
            // no usage at all, and the runtime's own estimate is not a measurement, so it
            // is shown as an estimate with its source rather than as a token count.
            // `null` is the runtime's way of saying "the provider reported nothing", so it
            // is treated exactly like a missing value: rendering it as "0 tokens" would
            // turn "unknown" into a measurement.
            tokens_reported: (tokens == null && completion == null)
              ? (payload.context_estimated_tokens
                 ? `~${payload.context_estimated_tokens} prompt tokens estimated ` +
                   `(the provider reported no usage)`
                 : null)
              : `${tokens ?? 0} prompt + ${completion ?? 0} completion`,
            test_double: Boolean(payload.is_test_double),
          },
        });
        break;
      }
      case "tool_call":
        messages.push({
          role: "tool", at: event.ts, title: payload.tool ?? "tool",
          body: payload.denied ? `refused: ${payload.error ?? "no reason recorded"}`
                               : (payload.ok ? "ok" : `failed: ${payload.error ?? ""}`),
          meta: { category: payload.category, purpose: payload.purpose, step: payload.step,
                  denied: Boolean(payload.denied), ok: Boolean(payload.ok) },
        });
        // A refusal by the permission engine is a refusal, even though it arrives as a
        // tool call that did not happen. It is counted with the boundary violations, and
        // the source says which mechanism refused.
        if (payload.denied) {
          refusals.push({ at: event.ts, source: "permission engine", tool: payload.tool,
                          category: payload.category,
                          reason: payload.error ?? "no reason recorded" });
        }
        break;
      case "tool_write": {
        const absolute = String(payload.path ?? "");
        const where = relativeToWorkspace(backendEnv, absolute);
        const location = artifactLocation(backendEnv, absolute);
        artifacts.push({ ...where, ...location, bytes: payload.bytes, sha256: payload.sha256,
                         purpose: payload.purpose, at: event.ts,
                         verified_by_readback: payload.verified_by_readback ?? null });
        messages.push({ role: "artifact", at: event.ts, title: where.path,
                        body: `wrote ${payload.bytes ?? "?"} byte(s)` +
                              (payload.sha256 ? `, sha256 ${String(payload.sha256).slice(0, 16)}…` : ""),
                        meta: { in_workspace: where.in_workspace, ...location } });
        break;
      }
      case "boundary_violation": {
        refusals.push({ at: event.ts, source: "boundary", category: payload.category,
                        reason: payload.reason, outcome: payload.outcome,
                        approval_id: payload.approval_id });
        messages.push({ role: "refusal", at: event.ts, title: payload.category ?? "refused",
                        body: String(payload.reason ?? ""),
                        meta: { outcome: payload.outcome, approval_id: payload.approval_id } });
        break;
      }
      case "task_verified":
        messages.push({ role: "system", at: event.ts, title: "acceptance checks",
                        body: `${payload.passed ?? 0} of ${payload.total ?? 0} criteria passed`,
                        meta: {} });
        break;
      case "task_finished":
        messages.push({ role: "system", at: event.ts, title: "task finished",
                        body: String(payload.summary ?? ""),
                        meta: { status: payload.status, unverified: payload.unverified } });
        break;
      case "task_run_complete":
        messages.push({ role: "system", at: event.ts, title: "run complete",
                        body: `${payload.status}; ${payload.steps ?? 0} step(s), ` +
                              `${payload.model_calls ?? 0} model call(s), ` +
                              `${payload.tool_calls ?? 0} tool call(s), ` +
                              `${payload.denials ?? 0} refusal(s)`,
                        meta: { stopped_because: payload.stopped_because, error: payload.error } });
        break;
      case "discovery_captured":
        messages.push({ role: "system", at: event.ts, title: "observation kept for later",
                        body: String(payload.title ?? ""),
                        meta: { discovery_id: payload.discovery_id } });
        break;
      case "capability_proposed":
      case "capability_scaffolded":
      case "capability_tested":
      case "capability_test_refused":
      case "capability_promoted":
      case "capability_maintained":
      case "capability_deprecated":
      case "capability_rollback":
        messages.push({ role: "system", at: event.ts, title: event.type.replace(/_/g, " "),
                        body: String(payload.name ?? payload.capability ?? ""),
                        meta: { status: payload.status, reason: payload.reason,
                                manifest_sha256: payload.manifest_sha256 } });
        break;
      case "approval_requested":
        messages.push({ role: "refusal", at: event.ts, title: "waiting for a human",
                        body: String(payload.reason ?? "an operation needs a human decision"),
                        meta: { tool: payload.tool, approval_id: payload.approval_id } });
        break;
      default:
        break;
    }
  }
  const state = verificationOf(task, { verification });
  return {
    task_id: taskId, task, steps, verification, criteria: criteriaOf(task, { verification }),
    verification_state: state, verification_label: verificationLabel(state),
    messages, artifacts, refusals,
    counts: {
      messages: messages.length, artifacts: artifacts.length, refusals: refusals.length,
      events_considered: events.length,
    },
  };
}

/** The completion summary is JSON text in the task row; `{}` means it never finished. */
export function summaryOf(task) {
  const raw = task?.summary;
  if (!raw || typeof raw !== "string") return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * What the runtime itself recorded about the acceptance checks. A task that has not
 * finished carries no summary, and a task with no acceptance criteria is not "verified"
 * -- it is unverified, and it has to say so, because the whole point of the criterion
 * machinery is that a completion claim is checked by something other than the claim.
 */
export function verificationOf(task, detail) {
  const summary = summaryOf(task);
  const block = summary?.verification ?? null;
  if (block && typeof block === "object") {
    const filled = { ...block, source: "the task's own completion summary" };
    if (!filled.state) {
      // A summary that counted nothing has not verified anything. Filling the gap with
      // "verified" because the task closed would be the exact claim this machinery exists
      // to prevent, so the default is the honest one.
      const counts = filled.counts ?? {};
      filled.total = filled.total ?? 0;
      filled.passed = filled.passed ?? counts.passed ?? 0;
      filled.state = filled.total === 0
        ? "unverified"
        : (Object.keys(counts).some((key) => key !== "passed") ? "not verified" : "verified");
    }
    return filled;
  }
  const checks = Array.isArray(detail?.verification) ? detail.verification : [];
  if (!checks.length) {
    return { state: task?.status === "complete" ? "unverified" : "not run", total: 0,
             passed: 0, failed: 0, counts: {}, source: "no acceptance check was recorded" };
  }
  const counts = {};
  for (const check of checks) counts[check.outcome] = (counts[check.outcome] ?? 0) + 1;
  const passed = counts.passed ?? 0;
  const failed = Object.entries(counts).filter(([key]) => key !== "passed")
                       .reduce((sum, [, value]) => sum + value, 0);
  const state = failed === 0 ? "verified" : (passed > 0 ? "partly verified" : "not verified");
  return { state, total: checks.length, passed, failed, counts,
           source: "the verification rows for this task" };
}

export function verificationLabel(block) {
  if (!block) return null;
  const total = block.total ?? 0;
  if (!total) return block.state ?? null;
  return `${block.state ?? "unverified"} ${block.passed ?? 0}/${total}`;
}

/** The criteria as written, each with the outcome the runtime recorded for it. */
export function criteriaOf(task, detail) {
  let specs = [];
  try {
    const parsed = JSON.parse(task?.acceptance ?? "[]");
    if (Array.isArray(parsed)) specs = parsed;
  } catch {
    specs = [];
  }
  const checks = Array.isArray(detail?.verification) ? detail.verification : [];
  const used = new Set();
  return specs.map((spec, index) => {
    let check = checks.find((row, position) => !used.has(position) &&
      row.kind === spec?.kind && row.spec === JSON.stringify(spec));
    if (!check) {
      check = checks.find((row, position) => !used.has(position) && row.kind === spec?.kind);
    }
    if (!check && checks[index] && !used.has(index)) check = checks[index];
    if (check) used.add(checks.indexOf(check));
    return { spec, outcome: check?.outcome ?? null, detail: check?.detail ?? null };
  });
}
