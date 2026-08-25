const REVIEWABLE_EXTENSIONS = new Set([
  "c", "cc", "cfg", "conf", "cpp", "cs", "css", "csv", "cxx", "env", "go", "graphql", "gql",
  "h", "hpp", "htm", "html", "ini", "java", "js", "json", "jsonc", "jsonl", "jsx", "kt", "kts",
  "less", "lock", "log", "lua", "md", "mjs", "php", "properties", "ps1", "py", "pyi", "rb", "rs",
  "rst", "sass", "scss", "sh", "sql", "svelte", "swift", "toml", "ts", "tsx", "txt", "vue", "xml",
  "yaml", "yml",
]);
const REVIEWABLE_FILENAMES = new Set([
  ".env", ".gitattributes", ".gitignore", "dockerfile", "license", "makefile", "readme",
]);

export interface WorkspaceFileReference {
  relativePath: string;
  line?: number;
  column?: number;
}

export interface ChangedWorkspaceFile {
  path: string;
  status?: string;
}

/** Returns a safe project-relative path for inline Markdown file references. */
export function resolveWorkspaceFileReference(value: string, workspace: string): WorkspaceFileReference | null {
  if (!workspace) return null;
  let candidate = value.trim();
  if (!candidate || candidate.includes("\n") || candidate.includes("\r")) return null;
  if ((candidate.startsWith('"') && candidate.endsWith('"'))
    || (candidate.startsWith("'") && candidate.endsWith("'"))) {
    candidate = candidate.slice(1, -1).trim();
  }
  candidate = decodeLocalPath(candidate);

  let line: number | undefined;
  let column: number | undefined;
  const hashLocation = candidate.match(/#L(\d+)(?:C(\d+))?$/i);
  const colonLocation = candidate.match(/:(\d+)(?::(\d+))?$/);
  const location = hashLocation ?? colonLocation;
  if (location) {
    line = Number(location[1]);
    column = location[2] ? Number(location[2]) : undefined;
    candidate = candidate.slice(0, -location[0].length);
  }

  if (!candidate || /[\0<>|?*]/.test(candidate)) return null;
  const normalizedWorkspace = normalizeAbsolutePath(workspace);
  let normalizedCandidate = candidate.replace(/\\/g, "/");
  const absolute = isAbsolutePath(normalizedCandidate);
  if (absolute) {
    normalizedCandidate = normalizeAbsolutePath(normalizedCandidate);
    const insensitive = /^[a-z]:\//i.test(normalizedWorkspace) || normalizedWorkspace.startsWith("//");
    const comparableWorkspace = insensitive ? normalizedWorkspace.toLocaleLowerCase() : normalizedWorkspace;
    const comparableCandidate = insensitive ? normalizedCandidate.toLocaleLowerCase() : normalizedCandidate;
    if (comparableCandidate === comparableWorkspace) return null;
    if (!comparableCandidate.startsWith(`${comparableWorkspace}/`)) return null;
    normalizedCandidate = normalizedCandidate.slice(normalizedWorkspace.length + 1);
  }

  const parts = normalizedCandidate.split("/").filter((part) => part && part !== ".");
  if (parts.length === 0 || parts.some((part) => part === ".." || part.includes(":"))) return null;
  const relativePath = parts.join("/");
  if (!isReviewableFileName(parts[parts.length - 1])) return null;
  return { relativePath, line, column };
}

/**
 * Reconciles the path printed by the model with the canonical paths recorded by
 * the task snapshot. Models often mention only a basename (for example
 * `health_check.py`) even though the write happened below a directory such as
 * `onboarding-demo/health_check.py`. Only a unique suffix match is accepted so
 * two same-named files can never cause the viewer to open the wrong file.
 */
export function reconcileChangedWorkspaceFile(
  reference: WorkspaceFileReference,
  changedFiles: readonly ChangedWorkspaceFile[],
): WorkspaceFileReference {
  const requested = normalizeRelativePath(reference.relativePath);
  if (!requested) return reference;
  const available = changedFiles
    .map((file) => ({ ...file, normalized: normalizeRelativePath(file.path) }))
    .filter((file) => Boolean(file.normalized));

  const exact = available.find((file) => sameWorkspacePath(file.normalized, requested));
  if (exact) return { ...reference, relativePath: exact.path };

  const suffixMatches = available.filter((file) => (
    file.normalized.length > requested.length
    && file.normalized.toLocaleLowerCase().endsWith(`/${requested.toLocaleLowerCase()}`)
  ));
  if (suffixMatches.length !== 1) return reference;
  return { ...reference, relativePath: suffixMatches[0].path };
}

export function codeLanguageFromClassName(className?: string) {
  const match = className?.match(/(?:^|\s)language-([^\s]+)/i);
  return (match?.[1] || "text").toLocaleLowerCase();
}

export function codeLanguageLabel(language: string) {
  const labels: Record<string, string> = {
    bash: "Bash",
    c: "C",
    cpp: "C++",
    csharp: "C#",
    css: "CSS",
    diff: "Diff",
    go: "Go",
    html: "HTML",
    java: "Java",
    javascript: "JavaScript",
    js: "JavaScript",
    json: "JSON",
    jsonl: "JSONL",
    jsx: "JavaScript React",
    markdown: "Markdown",
    md: "Markdown",
    patch: "Diff",
    powershell: "PowerShell",
    ps1: "PowerShell",
    py: "Python",
    python: "Python",
    rust: "Rust",
    rs: "Rust",
    shell: "Shell",
    sh: "Shell",
    sql: "SQL",
    text: "Plain text",
    toml: "TOML",
    ts: "TypeScript",
    tsx: "TypeScript React",
    typescript: "TypeScript",
    xml: "XML",
    yaml: "YAML",
    yml: "YAML",
  };
  return labels[language] ?? language.replace(/(^|[-_])(\w)/g, (_, __, letter: string) => ` ${letter.toUpperCase()}`).trim();
}

export function isDiffLanguage(language: string) {
  return language === "diff" || language === "patch" || language === "udiff";
}

function isReviewableFileName(fileName: string) {
  const lower = fileName.toLocaleLowerCase();
  if (REVIEWABLE_FILENAMES.has(lower)
    || [...REVIEWABLE_FILENAMES].some((name) => lower.startsWith(`${name}.`))) return true;
  const extensionIndex = lower.lastIndexOf(".");
  return extensionIndex > 0 && REVIEWABLE_EXTENSIONS.has(lower.slice(extensionIndex + 1));
}

function decodeLocalPath(value: string) {
  let decoded = value;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    // Keep the literal Markdown value when it is not URI encoded.
  }
  if (/^file:\/\//i.test(decoded)) {
    decoded = decoded.replace(/^file:\/\/(?:localhost)?/i, "");
    if (/^\/[a-z]:\//i.test(decoded)) decoded = decoded.slice(1);
  }
  return decoded;
}

function isAbsolutePath(value: string) {
  return /^[a-z]:\//i.test(value) || value.startsWith("/") || value.startsWith("//");
}

function normalizeAbsolutePath(value: string) {
  let normalized = value.replace(/\\/g, "/").replace(/\/+$/, "");
  if (/^\/[a-z]:\//i.test(normalized)) normalized = normalized.slice(1);
  return normalized;
}

function normalizeRelativePath(value: string) {
  return value.replace(/\\/g, "/").replace(/^\.\//, "").replace(/\/+$/, "");
}

function sameWorkspacePath(left: string, right: string) {
  return left.toLocaleLowerCase() === right.toLocaleLowerCase();
}
