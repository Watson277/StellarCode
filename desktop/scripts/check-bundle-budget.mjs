import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";

const dist = new URL("../dist/", import.meta.url);
const manifest = JSON.parse(await readFile(new URL(".vite/manifest.json", dist), "utf8"));
const initialChunks = new Set();
function visit(key) {
  if (initialChunks.has(key)) return;
  assert.ok(manifest[key], `Missing manifest entry: ${key}`);
  initialChunks.add(key);
  for (const dependency of manifest[key].imports ?? []) visit(dependency);
}
visit("index.html");
let bytes = 0;
for (const key of initialChunks) {
  const file = manifest[key].file;
  if (file.endsWith(".js")) bytes += (await stat(new URL(file, dist))).size;
}
assert.ok(bytes <= 500_000, `Initial JavaScript is ${bytes} bytes; budget is 500000. Check eager imports.`);
for (const name of ["SettingsPage", "MarkdownContent"]) {
  const key = Object.keys(manifest).find((entry) => entry.endsWith(`/${name}.tsx`));
  assert.ok(key && !initialChunks.has(key), `${name} must stay outside the initial static import graph`);
}
console.log(`Initial JavaScript: ${bytes} / 500000 bytes. Lazy-load boundaries verified.`);
