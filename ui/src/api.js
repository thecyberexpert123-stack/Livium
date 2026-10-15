// The Node Studio is the only browser-facing server. All calls are same-origin;
// its /api proxy talks to the Python runtime and its /studio/api routes manage the
// console. Nothing in the browser knows a backend port or holds a provider key.
const WRITE_HEADER = { "X-Livium-Studio": "1" };

async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { Accept: "application/json", ...options.headers },
  });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; }
  catch { data = { error: text.slice(0, 400) || "invalid JSON from the Studio" }; }
  if (!response.ok) {
    const error = new Error(data.error || `HTTP ${response.status} ${response.statusText}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

export const get = (path) => request(path);
export const write = (path, body) => request(path, {
  method: "POST", headers: { ...WRITE_HEADER, "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
export const remove = (path) => request(path, { method: "DELETE", headers: WRITE_HEADER });

export function workspaceUrl(root, path = "", extra = "") {
  return `/studio/api/workspace/file?root=${encodeURIComponent(root)}` +
    `&path=${encodeURIComponent(path)}${extra}`;
}

export async function upload(root, directory, files) {
  const placed = [];
  for (const file of files) {
    // The server returns the actual, contained path it accepted. In the chat
    // composer that path is named in the objective; never assume the browser's
    // filename survived server-side normalisation unchanged.
    placed.push(await request(`/studio/api/workspace/file?root=${encodeURIComponent(root)}`, {
      method: "POST",
      headers: {
        // HTTP headers cannot carry arbitrary Unicode filenames. The server
        // decodes this UTF-8 URI component before its safe-upload-name check.
        ...WRITE_HEADER, "X-Livium-Path": encodeURIComponent(file.name),
        "X-Livium-Directory": directory, "Content-Type": "application/octet-stream",
      },
      body: file,
    }));
  }
  return placed;
}

export async function imageBlobUrl(root, path) {
  const response = await fetch(workspaceUrl(root, path, "&download=1"));
  if (!response.ok) throw new Error(`image download failed (HTTP ${response.status})`);
  return URL.createObjectURL(await response.blob());
}
