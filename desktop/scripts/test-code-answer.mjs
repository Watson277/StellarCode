import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const sourceUrl = new URL("../src/features/markdown/codeAnswer.ts", import.meta.url);
const source = await readFile(sourceUrl, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
  fileName: sourceUrl.pathname,
}).outputText;
const answer = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

const workspace = "E:\\study2\\project";
assert.deepEqual(answer.resolveWorkspaceFileReference("src/app.py:12:4", workspace), {
  relativePath: "src/app.py",
  line: 12,
  column: 4,
});
assert.deepEqual(answer.resolveWorkspaceFileReference("E:\\study2\\project\\src\\App.tsx#L25", workspace), {
  relativePath: "src/App.tsx",
  line: 25,
  column: undefined,
});
assert.deepEqual(answer.resolveWorkspaceFileReference("README.md", workspace), {
  relativePath: "README.md",
  line: undefined,
  column: undefined,
});
assert.equal(answer.resolveWorkspaceFileReference("E:\\outside\\secret.py", workspace), null);
assert.equal(answer.resolveWorkspaceFileReference("../secret.py", workspace), null);
assert.equal(answer.resolveWorkspaceFileReference("npm run build", workspace), null);
assert.equal(answer.resolveWorkspaceFileReference("archive.zip", workspace), null);
assert.deepEqual(answer.reconcileChangedWorkspaceFile(
  { relativePath: "health_check.py", line: 8 },
  [{ path: "onboarding-demo/health_check.py", status: "created" }],
), {
  relativePath: "onboarding-demo/health_check.py",
  line: 8,
});
assert.deepEqual(answer.reconcileChangedWorkspaceFile(
  { relativePath: "components/ArticleCard.vue" },
  [{ path: "yanbian-blog/src/components/ArticleCard.vue", status: "modified" }],
), {
  relativePath: "yanbian-blog/src/components/ArticleCard.vue",
});
assert.deepEqual(answer.reconcileChangedWorkspaceFile(
  { relativePath: "SRC/App.tsx" },
  [{ path: "src/App.tsx", status: "modified" }],
), {
  relativePath: "src/App.tsx",
});
assert.deepEqual(answer.reconcileChangedWorkspaceFile(
  { relativePath: "app.py" },
  [
    { path: "backend/app.py", status: "created" },
    { path: "scripts/app.py", status: "created" },
  ],
), {
  relativePath: "app.py",
});
assert.deepEqual(answer.reconcileChangedWorkspaceFile(
  { relativePath: "removed.py" },
  [{ path: "legacy/removed.py", status: "deleted" }],
), {
  relativePath: "legacy/removed.py",
});
assert.equal(answer.codeLanguageFromClassName("foo language-typescript bar"), "typescript");
assert.equal(answer.codeLanguageLabel("tsx"), "TypeScript React");
assert.equal(answer.isDiffLanguage("patch"), true);

console.log("Code answer helpers: all assertions passed");
