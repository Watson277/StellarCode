import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const sourceUrl = new URL("../src/runtime/workspaceLayout.ts", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
  fileName: sourceUrl.pathname,
}).outputText;
const layout = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

const memory = new Map();
const storage = {
  getItem: (key) => memory.get(key) ?? null,
  setItem: (key, value) => memory.set(key, value),
};

assert.deepEqual(layout.readWorkspaceLayout(storage, 1380), layout.DEFAULT_WORKSPACE_LAYOUT);
layout.writeWorkspaceLayout(storage, { leftWidth: 300, rightWidth: 340, leftCollapsed: true, rightCollapsed: false });
assert.deepEqual(layout.readWorkspaceLayout(storage, 1380), {
  leftWidth: 300,
  rightWidth: 340,
  leftCollapsed: true,
  rightCollapsed: false,
});

memory.set(layout.WORKSPACE_LAYOUT_STORAGE_KEY, "not-json");
assert.deepEqual(layout.readWorkspaceLayout(storage, 1380), layout.DEFAULT_WORKSPACE_LAYOUT);

const narrowed = layout.fitWorkspaceLayout(layout.DEFAULT_WORKSPACE_LAYOUT, 760);
assert.equal(narrowed.leftWidth, 210);
assert.equal(narrowed.rightWidth, 220);

const resizedLeft = layout.resizeWorkspaceColumn(layout.DEFAULT_WORKSPACE_LAYOUT, "left", 1000, 1380);
assert.equal(resizedLeft.leftWidth, 420);
const resizedRight = layout.resizeWorkspaceColumn(layout.DEFAULT_WORKSPACE_LAYOUT, "right", 46, 1380);
assert.equal(resizedRight.rightWidth, 220);
const expandedRight = layout.resizeWorkspaceColumn(layout.DEFAULT_WORKSPACE_LAYOUT, "right", -400, 1380);
assert.equal(expandedRight.rightWidth, 666);
assert.equal(layout.workspaceColumnMaximum(layout.DEFAULT_WORKSPACE_LAYOUT, "right", 1380), 814);
const viewportWideRight = layout.resizeWorkspaceColumn(layout.DEFAULT_WORKSPACE_LAYOUT, "right", -2000, 1380);
assert.equal(viewportWideRight.rightWidth, 814);

const collapsed = layout.toggleWorkspaceColumn(layout.DEFAULT_WORKSPACE_LAYOUT, "right", 760);
assert.equal(collapsed.rightCollapsed, true);
assert.equal(collapsed.rightWidth, layout.DEFAULT_WORKSPACE_LAYOUT.rightWidth);
assert.equal(collapsed.leftWidth, layout.DEFAULT_WORKSPACE_LAYOUT.leftWidth);

const restored = layout.toggleWorkspaceColumn(collapsed, "right", 760);
assert.equal(restored.rightCollapsed, false);
assert.equal(restored.leftWidth + restored.rightWidth, 430);

const reviewLayout = layout.showWorkspaceColumn(
  { ...layout.DEFAULT_WORKSPACE_LAYOUT, rightCollapsed: true },
  "right",
  1380,
  640,
);
assert.equal(reviewLayout.rightCollapsed, false);
assert.equal(reviewLayout.rightWidth, 640);

console.log("Workspace layout state: all assertions passed");
