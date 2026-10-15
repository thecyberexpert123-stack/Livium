// The checked-in bundle is a deliverable, not a cache: its source stamp lets
// `node --test` detect a stale build even if the assets themselves still return 200.
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));

async function sourceFiles(directory) {
  const files = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await sourceFiles(absolute));
    else if (entry.isFile()) files.push(absolute);
  }
  return files;
}

export async function sourceDigest() {
  const listed = await sourceFiles(path.join(ROOT, "src"));
  const files = [...listed, ...["index.html", "package.json", "package-lock.json",
                             "vite.config.mjs", "build-stamp.mjs"].map(name => path.join(ROOT, name))]
    .sort();
  const digest = crypto.createHash("sha256");
  for (const filename of files) {
    digest.update(path.relative(ROOT, filename));
    digest.update("\0");
    digest.update(await fs.readFile(filename));
    digest.update("\0");
  }
  return digest.digest("hex");
}
