import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const sourceUrl = new URL("../src/runtime/sessionDraftStore.ts", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2020,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: sourceUrl.pathname,
}).outputText;
const { SessionDraftStore } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

const store = new SessionDraftStore();
const attachments = [{
  id: "image-1",
  kind: "image",
  mime_type: "image/png",
  display_name: "clipboard.png",
  data_base64: "base64-payload",
}];

const saved = store.save("session-1", { prompt: "draft", attachments });
attachments[0].display_name = "mutated-input.png";
attachments.push({
  id: "file-2",
  kind: "file",
  mime_type: "text/plain",
  display_name: "extra.txt",
});
saved.prompt = "mutated-output";
saved.attachments[0].display_name = "mutated-output.png";
saved.attachments.length = 0;

const restored = store.get("session-1");
assert.equal(restored.prompt, "draft");
assert.equal(restored.attachments.length, 1);
assert.equal(restored.attachments[0].display_name, "clipboard.png");
assert.equal(restored.attachments[0].data_base64, "base64-payload");

restored.attachments[0].display_name = "mutated-restored.png";
assert.equal(store.get("session-1").attachments[0].display_name, "clipboard.png");

assert.deepEqual(store.clear("session-1"), { prompt: "", attachments: [] });
assert.deepEqual(store.get("session-1"), { prompt: "", attachments: [] });
assert.equal(store.delete("session-1"), true);
assert.equal(store.get("session-1"), undefined);
assert.throws(
  () => store.save(" ", { prompt: "invalid", attachments: [] }),
  /non-empty session id/,
);

console.log("SessionDraftStore: all assertions passed");
