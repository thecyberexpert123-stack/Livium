// The containment rules, tested against the two halves separately.
//
// The point of these tests is the second half: a textual check that only looks at the
// string happily serves a symlink that points at /etc/passwd. Each refusal below is a
// request the Studio would otherwise turn into a filesystem call in its own process.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { PathRefused, listDirectory, readFile, realUnder, resolveUnder, safeUploadName,
         segments } from "../lib/paths.mjs";

async function scratch() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "livium-paths-"));
  await fs.mkdir(path.join(root, "workspace", "sub"), { recursive: true });
  return { root, workspace: path.join(root, "workspace") };
}

async function symlinkOrSkip(t, target, link) {
  try {
    await fs.symlink(target, link);
    return true;
  } catch (error) {
    if (process.platform === "win32" && ["EPERM", "EACCES"].includes(error.code)) {
      t.skip("Windows symlink creation requires Developer Mode or elevated privileges");
      return false;
    }
    throw error;
  }
}

test("segments refuses a path that is not relative", () => {
  assert.throws(() => segments("/etc/passwd"), PathRefused);
  assert.throws(() => segments("C:\\Windows\\system32"), PathRefused);
  assert.throws(() => segments("a\0b"), PathRefused);
});

test("segments refuses climbing out, however it is spelled", () => {
  for (const attempt of ["..", "../x", "a/../../b", "a\\..\\..\\b", "sub/../../etc"]) {
    assert.throws(() => segments(attempt), PathRefused, attempt);
  }
  assert.deepEqual(segments("sub/./file.txt"), ["sub", "file.txt"]);
  assert.deepEqual(segments("sub\\file.txt"), ["sub", "file.txt"]);
  assert.deepEqual(segments(""), []);
});

test("resolveUnder keeps the result under the root", async () => {
  const { workspace } = await scratch();
  assert.equal(resolveUnder(workspace, "sub/file.txt"), path.join(workspace, "sub", "file.txt"));
  assert.equal(resolveUnder(workspace, ""), workspace);
  assert.throws(() => resolveUnder(workspace, "../outside.txt"), PathRefused);
});

test("a symlink out of the workspace is refused once it is followed", async (t) => {
  const { root, workspace } = await scratch();
  const outside = path.join(root, "outside.txt");
  await fs.writeFile(outside, "not the workspace's business\n");
  if (!await symlinkOrSkip(t, outside, path.join(workspace, "escape.txt"))) return;
  if (!await symlinkOrSkip(t, "/etc", path.join(workspace, "etc"))) return;

  // The textual check passes -- the name is inside the workspace. The resolved check
  // is the one that refuses, and it is the one that matters.
  assert.equal(resolveUnder(workspace, "escape.txt"), path.join(workspace, "escape.txt"));
  await assert.rejects(() => realUnder(workspace, "escape.txt"), PathRefused);
  await assert.rejects(() => readFile(workspace, "escape.txt"), PathRefused);
  await assert.rejects(() => listDirectory(workspace, "etc"), PathRefused);
});

test("a symlink that stays inside the workspace is allowed", async (t) => {
  const { workspace } = await scratch();
  await fs.writeFile(path.join(workspace, "sub", "real.txt"), "inside\n");
  if (!await symlinkOrSkip(t, path.join(workspace, "sub", "real.txt"),
                           path.join(workspace, "link.txt"))) return;
  assert.equal(await realUnder(workspace, "link.txt"), path.join(workspace, "sub", "real.txt"));
});

test("realUnder resolves a path that does not exist yet, for uploads", async () => {
  const { workspace } = await scratch();
  const target = await realUnder(workspace, "sub/new/not-made-yet.txt");
  assert.equal(target, path.join(workspace, "sub", "new", "not-made-yet.txt"));
});

test("safeUploadName takes a name, never a path", () => {
  assert.equal(safeUploadName("report.txt"), "report.txt");
  assert.equal(safeUploadName("  report.txt  "), "report.txt");
  assert.equal(safeUploadName("uploads/report.txt"), "report.txt");
  assert.equal(safeUploadName("..\\..\\report.txt"), "report.txt");
  assert.equal(safeUploadName("re\u0000port\u001b.txt"), "report.txt");
  assert.throws(() => safeUploadName(""), PathRefused);
  assert.throws(() => safeUploadName(".."), PathRefused);
  assert.throws(() => safeUploadName("."), PathRefused);
  assert.throws(() => safeUploadName("dir/"), PathRefused);
  assert.throws(() => safeUploadName("x".repeat(129)), PathRefused);
});

test("listDirectory reports what is there, and hides dotfiles unless asked", async () => {
  const { workspace } = await scratch();
  await fs.writeFile(path.join(workspace, "b.txt"), "b\n");
  await fs.writeFile(path.join(workspace, ".env"), "SECRET=1\n");
  await fs.mkdir(path.join(workspace, "adir"));

  const plain = await listDirectory(workspace, "");
  assert.deepEqual(plain.entries.map((entry) => entry.name), ["adir", "sub", "b.txt"]);
  assert.equal(plain.entries[0].kind, "directory");
  assert.equal(plain.entries[2].kind, "file");
  assert.equal(plain.entries[2].size, 2);
  assert.match(plain.entries[2].modified, /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(plain.path, "");

  const all = await listDirectory(workspace, "", { hidden: true });
  assert.deepEqual(all.entries.map((entry) => entry.name), ["adir", "sub", ".env", "b.txt"]);

  await assert.rejects(() => listDirectory(workspace, "b.txt"), PathRefused);
});

test("readFile refuses what is not a regular file and flags truncation", async () => {
  const { workspace } = await scratch();
  await fs.writeFile(path.join(workspace, "small.txt"), "hello\n");
  const info = await readFile(workspace, "small.txt", { maxBytes: 1024 });
  assert.equal(info.name, "small.txt");
  assert.equal(info.size, 6);
  assert.equal(info.truncated, false);

  const big = await readFile(workspace, "small.txt", { maxBytes: 2 });
  assert.equal(big.truncated, true);
  assert.equal(big.maxBytes, 2);

  await assert.rejects(() => readFile(workspace, "sub"), PathRefused);
});
