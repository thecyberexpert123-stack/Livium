// The page and its script have to agree about the same element ids.
//
// This is the check that would have caught the original defect: the page referred to
// `/static/app.js`, the server looked for `public/static/app.js`, and the console loaded
// as HTML with no behaviour at all — every API test still passed. So this test is static
// on purpose: it reads the two files and asserts that each other's assumptions hold.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(HERE, "..", "public");
const page = await fs.readFile(path.join(PUBLIC, "index.html"), "utf8");
const script = await fs.readFile(path.join(PUBLIC, "app.js"), "utf8");
const styles = await fs.readFile(path.join(PUBLIC, "styles.css"), "utf8");

//: Ids the script creates while rendering, so they cannot be in the page up front.
const RUNTIME_IDS = new Set(["stop-run"]);

function idsIn(text, pattern) {
  const found = new Set();
  for (const match of text.matchAll(pattern)) found.add(match[1]);
  return found;
}

test("the page asks for the assets the server serves", () => {
  for (const asset of ["/static/styles.css", "/static/app.js"]) {
    assert.match(page, new RegExp(asset.replace(".", "\\.")), `the page does not reference ${asset}`);
  }
  assert.match(page, /<script type="module" src="\/static\/app\.js">/, "the script must be a module");
});

test("every element the script reaches for exists in the page", () => {
  const used = idsIn(script, /\$\("([^"]+)"\)/g);
  const declared = idsIn(page, /id="([^"]+)"/g);
  const missing = [...used].filter((id) => !declared.has(id) && !RUNTIME_IDS.has(id)).sort();
  assert.deepEqual(missing, [], `app.js reads ids that index.html does not define: ${missing}`);
});

test("the three regions of the shell are in the page", () => {
  const declared = idsIn(page, /id="([^"]+)"/g);
  for (const id of ["sidebar", "chat-title", "dock", "transcript", "composer"]) {
    assert.equal(declared.has(id), true, `the shell has no ${id}`);
  }
  // The workspace panel is the point of the right-hand column: it must be able to show a
  // file list and a preview, not just a title.
  for (const id of ["file-list", "file-preview", "root-select", "upload-btn"]) {
    assert.equal(declared.has(id), true, `the workspace panel has no ${id}`);
  }
});

test("no inline handlers: the script is the only place behaviour lives", () => {
  assert.doesNotMatch(page, /\son(click|change|submit|input|load)=/i);
});

test("the stylesheet covers the shell it lays out", () => {
  for (const selector of [".app", ".sidebar", ".chat", ".dock", ".composer", ".transcript", ".sheet", ".toast"]) {
    assert.match(styles, new RegExp(`\\${selector}\\b`), `${selector} is not styled`);
  }
  assert.match(styles, /\[data-theme="dark"\]/, "the dark theme tokens are missing");
  assert.match(styles, /@media \(max-width: 760px\)/, "there is no narrow-screen layout");
});
