import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const sourceUrl = new URL("../src/features/review/diffParser.ts", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2020,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: sourceUrl.pathname,
}).outputText;
const { buildReviewDiffFiles } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

const patch = `diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 print("before")
-old_value = 1
+new_value = 2
+enabled = True
 return old_value
diff --git a/notes.txt b/notes.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/notes.txt
@@ -0,0 +1,2 @@
+first
+second
diff --git a/removed.txt b/removed.txt
deleted file mode 100644
index 4444444..0000000
--- a/removed.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone`;

const files = buildReviewDiffFiles({
  diff: patch,
  changed_files: [
    { path: "src/app.py", status: "modified", additions: 2, deletions: 1 },
    { path: "notes.txt", status: "created", additions: 2, deletions: 0 },
    { path: "removed.txt", status: "deleted", additions: 0, deletions: 1 },
    { path: "image.png", status: "binary", additions: 0, deletions: 0 },
  ],
});

assert.equal(files.length, 4);
assert.equal(files[0].path, "src/app.py");
assert.equal(files[0].hunks.length, 1);
assert.deepEqual(
  files[0].hunks[0].lines.map((line) => [line.kind, line.oldLine, line.newLine]),
  [
    ["context", 1, 1],
    ["deletion", 2, undefined],
    ["addition", undefined, 2],
    ["addition", undefined, 3],
    ["context", 3, 4],
  ],
);
assert.equal(files[1].status, "created");
assert.equal(files[1].oldPath, "/dev/null");
assert.equal(files[2].status, "deleted");
assert.equal(files[2].newPath, "/dev/null");
assert.equal(files[3].binary, true);
assert.equal(files[3].patch, "");

const multiHunk = buildReviewDiffFiles({
  diff: `diff --git a/file with spaces.txt b/file with spaces.txt
--- a/file with spaces.txt
+++ b/file with spaces.txt
@@ -1 +1 @@
-before
+after
@@ -20,2 +20,3 @@ section
 keep
+inserted
 keep-again`,
  changed_files: [
    { path: "file with spaces.txt", status: "modified", additions: 2, deletions: 1 },
  ],
});
assert.equal(multiHunk[0].path, "file with spaces.txt");
assert.equal(multiHunk[0].hunks.length, 2);
assert.equal(multiHunk[0].hunks[1].lines[1].newLine, 21);

console.log("Review diff parser: all assertions passed");
