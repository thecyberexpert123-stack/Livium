import React, { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get, write } from "./api.js";
import { Badge, Failure, KV } from "./common.jsx";

function parseExtra(text) {
  const extra = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index < 0) throw new Error(`"${trimmed}" is not NAME=value`);
    extra[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim();
  }
  return extra;
}

export function Settings({ state, onState, onError }) {
  const [form, setForm] = useState({ provider: "openai_compatible", model: "", base_url: "",
                                      api_key: "", timeout: "120", maxOutput: "1024",
                                      reasoningBudget: "4096", extra: "" });
  const [dirty, setDirty] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [note, setNote] = useState("");
  const [validation, setValidation] = useState(null);
  const [busy, setBusy] = useState(false);
  const backendLog = useQuery({ queryKey: ["backend-log"],
    queryFn: () => get("/studio/api/backend/log"), refetchInterval: 8000 });
  const settings = state?.settings;
  useEffect(() => {
    if (!settings || dirty) return;
    setForm({ provider: settings.provider, model: settings.model,
              base_url: settings.base_url, api_key: "",
              timeout: String(settings.model_options?.timeout_seconds ?? 120),
              maxOutput: String(settings.model_options?.max_output_tokens ?? 1024),
              reasoningBudget: String(settings.model_options?.nvidia_reasoning_budget ?? 4096),
              extra: Object.keys(settings.extra ?? {}).map(key =>
                `# ${key} is set (value is write-only). Add ${key}=new-value to replace or ${key}= to clear.`
              ).join("\n") });
  }, [settings, dirty]);
  const set = (field, value) => { setDirty(true); setForm(previous => ({ ...previous, [field]: value })); };
  function chooseProvider(value) {
    setDirty(true);
    setForm(previous => {
      if (value !== "groq") return { ...previous, provider: value };
      return {
        ...previous,
        provider: value,
        base_url: "https://api.groq.com/openai/v1",
        model: !previous.model || previous.model.startsWith("nvidia/")
          ? "openai/gpt-oss-120b" : previous.model,
      };
    });
  }

  async function save({ restart = true } = {}) {
    if (busy) return;
    setBusy(true); setNote(""); setValidation(null);
    try {
      const timeout = Number(form.timeout);
      const maxOutput = Number(form.maxOutput);
      const reasoningBudget = Number(form.reasoningBudget);
      if (!Number.isInteger(timeout) || timeout < 10 || timeout > 900) {
        throw new Error("Request timeout must be between 10 and 900 seconds.");
      }
      if (!Number.isInteger(maxOutput) || maxOutput < 128 || maxOutput > 32768) {
        throw new Error("Response token limit must be between 128 and 32,768.");
      }
      if (!Number.isInteger(reasoningBudget) || reasoningBudget < 128 || reasoningBudget > 32768) {
        throw new Error("NVIDIA reasoning budget must be between 128 and 32,768 tokens.");
      }
      if (form.provider === "groq" && settings?.api_key_set && settings.provider !== "groq" && !form.api_key) {
        throw new Error("Enter your Groq API key before switching; the currently saved key belongs to another provider.");
      }
      const extra = parseExtra(form.extra);
      extra.LIVIUM_MODEL_TIMEOUT = String(timeout);
      extra.LIVIUM_MAX_OUTPUT_TOKENS = String(maxOutput);
      extra.LIVIUM_NVIDIA_REASONING_BUDGET = String(reasoningBudget);
      const patch = { provider: form.provider, model: form.model, base_url: form.base_url,
                      extra, restart };
      if (form.api_key) patch.api_key = form.api_key;
      const result = await write("/studio/api/settings", patch);
      setForm(previous => ({ ...previous, api_key: "" }));
      setDirty(false);
      setNote(result.note);
      setValidation(result.provider);
      await onState();
      backendLog.refetch();
    } catch (error) { setNote(`Not saved: ${error.message}`); onError(error.message); }
    finally { setBusy(false); }
  }
  async function clearKey() {
    if (!window.confirm("Remove the stored API key?")) return;
    try {
      const result = await write("/studio/api/settings", { clear_api_key: true, restart: true });
      setForm(previous => ({ ...previous, api_key: "" }));
      setNote(result.note); setValidation(result.provider);
      await onState();
    } catch (error) { onError(error.message); }
  }
  async function probe() {
    try {
      const next = await get("/studio/api/state?probe=1");
      setValidation(next.provider); onState(next);
      setNote("Fresh provider probe recorded by the runtime.");
    } catch (error) { onError(error.message); }
  }
  async function restart() {
    try {
      const result = await write("/studio/api/backend/restart", {});
      setValidation(result.provider); setNote("Runtime restarted; see its actual probe below.");
      await onState(); backendLog.refetch();
    } catch (error) { onError(error.message); }
  }
  const provider = validation ?? state?.provider ?? {};
  const nvidiaEndpoint = form.provider === "openai_compatible" && (() => {
    try { return new URL(form.base_url).hostname.toLowerCase() === "integrate.api.nvidia.com"; }
    catch { return form.model.toLowerCase().includes("nemotron-3-ultra-550b-a55b"); }
  })();
  const providerHelp = {
    openai_compatible: "For NVIDIA, OpenAI, OpenRouter, and compatible hosted or local APIs. Use the exact model ID and the provider’s API URL.",
    groq: "Groq uses its OpenAI-compatible API. Enter your Groq API key; choose an active model ID from Groq’s model catalog.",
    anthropic: "Uses Anthropic’s Messages API. Enter an Anthropic model ID; the API key stays private.",
    ollama: "For a local Ollama server. The usual URL is http://localhost:11434; a cloud API key is not needed.",
    scripted: "Development double only. It does not produce real model output and should not be used to evaluate Livium’s real task execution.",
  }[form.provider];

  return <div className="sheet-body">
    <div className="card wide model-health">
      <div><h4>Model connection</h4><p className="hint">{settings?.model || "No model configured"}
        {settings?.base_url ? ` · ${settings.base_url}` : ""}</p></div>
      <Badge tone={provider.ok === true ? "ok" : provider.ok === false ? "bad" : "warn"}>
        {provider.ok === true ? "Endpoint reachable" : provider.ok === false ? "Unavailable" : "Not checked"}
      </Badge>
      <p className="hint model-health-note">Reachable means the model-list endpoint answered. Send a short prompt to confirm generation works.</p>
    </div>

    <div className="card wide"><h4>Model setup</h4>
      <p className="hint">Set one provider and model. Credentials stay private; advanced runtime controls are below.</p>
      <form className="settings-grid" onSubmit={event => { event.preventDefault(); save(); }}>
        <label>Provider
          <select value={form.provider} onChange={event => chooseProvider(event.target.value)}>
            <option value="openai_compatible">OpenAI-compatible API</option>
            <option value="groq">Groq</option>
            <option value="anthropic">Anthropic</option>
            <option value="ollama">Ollama (local)</option>
            <option value="scripted">Scripted (development only)</option>
          </select>
        </label>
        <p className="hint provider-help">{providerHelp}</p>
        <label>Model ID <input value={form.model} onChange={event => set("model", event.target.value)}
          placeholder={form.provider === "groq" ? "openai/gpt-oss-120b" : form.provider === "anthropic" ? "claude-sonnet-4-5" : form.provider === "ollama" ? "llama3.1" : "provider/model-name"} /></label>
        <label>API base URL <input value={form.base_url} onChange={event => set("base_url", event.target.value)}
          placeholder={form.provider === "ollama" ? "http://localhost:11434" : "https://api.provider.com/v1"} />
          <em>Leave blank to use the provider default.</em></label>
        <label>Request timeout <em>seconds · allows slower model responses</em>
          <input type="number" min="10" max="900" step="1" value={form.timeout}
            onChange={event => set("timeout", event.target.value)} /></label>
        <label>Response token limit <em>maximum generated tokens per call</em>
          <input type="number" min="128" max="32768" step="1" value={form.maxOutput}
            onChange={event => set("maxOutput", event.target.value)} /></label>
        {nvidiaEndpoint && <label>NVIDIA reasoning budget <em>limits internal reasoning tokens per request</em>
          <input type="number" min="128" max="32768" step="1" value={form.reasoningBudget}
            onChange={event => set("reasoningBudget", event.target.value)} /></label>}
        <label className="wide">API key
          <span className="row"><input aria-label="API key" type={showKey ? "text" : "password"}
            autoComplete="off" value={form.api_key} onChange={event => set("api_key", event.target.value)}
            placeholder={settings?.api_key_set
              ? `a key is set (${settings.api_key_hint}); enter a new one to replace it`
              : "enter a key; it is never echoed back"} />
            <button type="button" className="ghost" onClick={() => setShowKey(!showKey)}>
              {showKey ? "hide" : "show"}</button>
            <button type="button" className="ghost" onClick={clearKey}>clear</button></span>
        </label>
        {form.provider === "groq" && settings?.api_key_set && settings?.provider !== "groq" &&
          <p className="hint provider-help">You’re switching providers. Enter your Groq key above to replace the currently stored key.</p>}
        <div className="row wide">
          <button type="submit" disabled={busy}>Save and restart the runtime</button>
          <button type="button" className="ghost" onClick={probe}>
            Re-probe now, without restarting</button>
          <button type="button" className="link" disabled={busy} onClick={() => save({ restart: false })}>
            Save without restarting</button>
        </div>
      </form>
      {note && <p className="hint" role="status">{note}</p>}
    </div>
    <details className="advanced-settings">
      <summary>Advanced diagnostics and environment</summary>
      <div className="grid-3">
      <div className="card"><h4>Runtime diagnostics</h4><KV rows={[
        ["configured", `${settings?.provider || "unknown"}${settings?.model ? ` / ${settings.model}` : ""}`],
        ["base url", settings?.base_url || "(provider default)"],
        ["api key", settings?.api_key_set ? `${settings.api_key_hint} · sha256 ${state?.key_fingerprint}` : "not set"],
        ["reachable", provider.ok == null ? "not probed" : String(provider.ok)],
        ["probe", provider.age_seconds == null ? "age unknown" : provider.fresh
          ? "taken just now" : `cached, taken ${provider.age_seconds}s ago`],
        ["detail", provider.detail],
        ["context window", provider.context_window && `${provider.context_window} (${provider.context_window_source})`],
        ["test double", provider.is_test_double === true ? "yes — no real model output" : "no"],
        ["isolation", state?.runtime?.isolation?.detail],
        ["runtime home", state?.studio?.home],
      ]} /><p className="hint">A probe is a real request. A failed probe is never
        silently replaced with the scripted double.</p></div>
      <div className="card"><h4>Extra environment</h4>
        <p className="hint">One <code>NAME=value</code> per line. Existing variable names
          are shown as comments; their values are write-only, so saving another setting
          does not erase them. Use <code>NAME=</code> to clear one. Test-double changes
          are called out in the health bar.</p>
        <textarea rows={6} aria-label="Extra environment" value={form.extra}
          onChange={event => set("extra", event.target.value)}
          placeholder={"LIVIUM_MAX_STEPS=12\nLIVIUM_FOLLOWUP_MAX_MODEL_CALLS=4"} /></div>
    </div>
    <div className="card wide"><h4>Runtime backend log</h4>
      <Failure error={backendLog.error} context="Backend log unavailable" />
      <pre className="log">{backendLog.data?.log || "(the runtime has logged nothing yet)"}</pre>
      <div className="row"><button className="ghost" type="button" onClick={restart}>
        restart the runtime</button><button type="button" className="link"
        onClick={() => backendLog.refetch()}>reload log</button></div>
    </div>
    </details>
  </div>;
}
