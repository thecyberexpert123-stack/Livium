// Path rules for the Studio.
//
// Every workspace path in the UI becomes a filesystem call in this process, so the
// containment rule lives in one place and is applied before anything touches the
// disk. The rule has two halves, and the second one is the one people forget:
//
//   1. the *textual* path must stay under the root after normalisation, and
//   2. the *resolved* path must stay under the root after symlinks are followed.
//
// A check that only does the first happily serves /workspace/link-to-etc-passwd,
// which is why the tests here write a symlink that points outside and assert it is
// refused.

import fs from "node:fs/promises";
import path from "node:path";

export class PathRefused extends Error {
  constructor(message) {
    super(message);
    this.name = "PathRefused";
  }
}

/** Split a user-supplied relative path into safe segments. */
export function segments(relative) {
  const raw = String(relative ?? "");
  if (raw.includes("\0")) throw new PathRefused("a path cannot contain a NUL byte");
  if (raw.startsWith("/") || /^[A-Za-z]:[\\/]/.test(raw)) {
    throw new PathRefused("a workspace path must be relative to the workspace root");
  }
  const parts = raw.split(/[\\/]+/).filter((part) => part !== "" && part !== ".");
  if (parts.some((part) => part === "..")) {
    throw new PathRefused("a workspace path cannot climb out of the workspace");
  }
  return parts;
}

/** The textual half: normalise, then require the result to be the root or below it. */
export function resolveUnder(root, relative) {
  const base = path.resolve(root);
  const joined = path.resolve(base, ...segments(relative));
  if (joined !== base && !joined.startsWith(base + path.sep)) {
    throw new PathRefused("the path resolves outside the workspace root");
  }
  return joined;
}

/** The resolved half: follow symlinks on whatever exists, then check containment. */
export async function realUnder(root, relative) {
  const base = await fs.realpath(path.resolve(root));
  const joined = resolveUnder(base, relative);
  let existing = joined;
  for (;;) {
    try {
      const real = await fs.realpath(existing);
      const tail = path.relative(existing, joined);
      const final = tail ? path.join(real, tail) : real;
      if (final !== base && !final.startsWith(base + path.sep)) {
        throw new PathRefused(
          "the path resolves outside the workspace root once symlinks are followed");
      }
      return final;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      const parent = path.dirname(existing);
      if (parent === existing) throw new PathRefused("the path has no existing ancestor");
      existing = parent;
    }
  }
}

/** A file name for an upload: no separators, no control characters, no surprises. */
export function safeUploadName(name) {
  const raw = String(name ?? "").trim();
  if (!raw) throw new PathRefused("an upload needs a file name");
  const base = raw.split(/[\\/]+/).pop() ?? "";
  // eslint-disable-next-line no-control-regex
  const cleaned = base.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  if (!cleaned || cleaned === "." || cleaned === "..") {
    throw new PathRefused(`refused a file name of ${JSON.stringify(raw)}`);
  }
  if (cleaned.length > 128) throw new PathRefused("the file name is longer than 128 characters");
  return cleaned;
}

function kindOf(stats) {
  if (stats.isDirectory()) return "directory";
  if (stats.isSymbolicLink()) return "symlink";
  return "file";
}

/** One directory level, with the facts the UI shows and nothing inferred. */
export async function listDirectory(root, relative = "", { hidden = false } = {}) {
  const base = await fs.realpath(path.resolve(root));
  const directory = await realUnder(base, relative);
  const stats = await fs.stat(directory);
  if (!stats.isDirectory()) throw new PathRefused(`${relative || "."} is not a directory`);
  const entries = await fs.readdir(directory, { withFileTypes: true });
  const out = [];
  for (const entry of entries) {
    if (!hidden && entry.name.startsWith(".")) continue;
    const absolute = path.join(directory, entry.name);
    let size = null;
    let modified = null;
    let kind = entry.isDirectory() ? "directory" : entry.isSymbolicLink() ? "symlink" : "file";
    try {
      const info = await fs.lstat(absolute);
      size = info.size;
      modified = info.mtime.toISOString();
      kind = kindOf(info);
    } catch {
      kind = "unreadable";
    }
    const rel = path.posix.join(relative ? relative.replace(/\\/g, "/") : "", entry.name);
    out.push({ name: entry.name, path: rel, kind, size, modified });
  }
  out.sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "directory" ? -1 : 1));
  return { path: relative.replace(/\\/g, "/"), entries: out };
}

/** Read a file for preview or download, refusing anything that is not a small file. */
export async function readFile(root, relative, { maxBytes = 512 * 1024 } = {}) {
  const base = await fs.realpath(path.resolve(root));
  const absolute = await realUnder(base, relative);
  const stats = await fs.stat(absolute);
  if (!stats.isFile()) throw new PathRefused(`${relative} is not a regular file`);
  return {
    absolute,
    name: path.basename(absolute),
    size: stats.size,
    modified: stats.mtime.toISOString(),
    truncated: stats.size > maxBytes,
    maxBytes,
  };
}
