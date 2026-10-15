import React from "react";

export function Badge({ tone = "", children }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function KV({ rows }) {
  return <dl className="kv">{rows.filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => <React.Fragment key={key}>
      <dt>{key}</dt><dd>{value === null ? "not reported" : String(value)}</dd>
    </React.Fragment>)}</dl>;
}

export function Failure({ error, context = "Could not load" }) {
  if (!error) return null;
  return <p className="failure" role="alert">{context}: {error.message}</p>;
}

export function formatBytes(value) {
  if (value == null) return "not reported";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} kB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

export function formatWhen(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export function healthBadges(state) {
  if (!state) return [<Badge key="load" tone="warn">reading runtime state…</Badge>];
  const provider = state.provider ?? {};
  const settings = state.settings ?? {};
  const backend = state.backend ?? {};
  const age = provider.fresh ? "just now" : provider.age_seconds == null
    ? "age unknown" : `${Math.round(provider.age_seconds)}s ago`;
  return [
    <Badge key="probe" tone={provider.ok === true ? "ok" : "bad"}>
      provider {provider.ok === true ? "reachable" : "not usable"} · probed {age}
    </Badge>,
    <Badge key="model" tone="accent">{settings.provider ?? "unconfigured"}
      {settings.model ? `:${settings.model}` : ""}</Badge>,
    <Badge key="runtime" tone={backend.running ? "ok" : "bad"}>
      runtime {backend.running ? `:${backend.port}` : "down"}</Badge>,
    <Badge key="key" tone={settings.api_key_set ? "ok" : "warn"}>
      key {settings.api_key_set ? settings.api_key_hint : "not set"}</Badge>,
    state.runtime?.isolation && <Badge key="network"
      tone={state.runtime.isolation.network_isolated ? "ok" : "warn"}>
      commands: network {state.runtime.isolation.network_isolated ? "isolated" : "open"}</Badge>,
    state.runtime?.isolation && <Badge key="filesystem"
      tone={state.runtime.isolation.filesystem_confined ? "ok" : "warn"}>
      commands: filesystem {state.runtime.isolation.filesystem_confined ? "confined" : "not confined"}
    </Badge>,
    (provider.is_test_double === true || (settings.warnings ?? []).length > 0)
      && <Badge key="double" tone="warn">scripted test double · no real model</Badge>,
  ].filter(Boolean);
}

export function stateWarnings(state) {
  if (!state) return [];
  return [
    state.backend?.last_error && `runtime backend failed to start: ${state.backend.last_error}`,
    state.error,
    state.provider?.ok === false && state.provider?.detail && `provider: ${state.provider.detail}`,
    ...(state.settings?.warnings ?? []),
  ].filter(Boolean);
}
