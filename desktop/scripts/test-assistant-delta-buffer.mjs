import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/runtime/assistantDeltaBuffer.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 } }).outputText;
const { AssistantDeltaBuffer } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
const batches = [];
const buffer = new AssistantDeltaBuffer((batch) => batches.push(batch), 10);
let sequence = 0;
const event = (text, extra = {}) => ({ type: "assistant.delta", session_id: "a", task_id: "task", runtime_pid: 1,
  event_id: `${++sequence}`, timestamp: "now", data: { text }, ...extra });
for (let index = 0; index < 100; index++) buffer.push(event("字"));
assert.equal(batches.length, 0);
buffer.flush();
assert.equal(batches.length, 1);
assert.equal(batches[0].length, 1);
assert.equal(batches[0][0].data.text, "字".repeat(100));
buffer.flush();
assert.equal(batches.length, 1);

buffer.push(event("old"));
buffer.push(event("", { data: { text: "", reset: true } }));
buffer.push(event("new"));
buffer.push(event("other", { session_id: "b" }));
buffer.push(event("process", { runtime_pid: 2 }));
buffer.flush();
assert.deepEqual(batches[1].map((item) => item.data.text), ["old", "", "new", "other", "process"]);
assert.equal(batches[1][1].data.reset, true);

buffer.push(event("scheduled"));
await new Promise((resolve) => setTimeout(resolve, 30));
assert.equal(batches.length, 3);
buffer.push(event("discard"));
buffer.dispose();
await new Promise((resolve) => setTimeout(resolve, 30));
assert.equal(batches.length, 3);
buffer.push(event("x".repeat(32768)));
assert.equal(batches.length, 4);
buffer.dispose();
console.log("Assistant delta batching: all assertions passed");
