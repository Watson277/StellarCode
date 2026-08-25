import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import groovy from "highlight.js/lib/languages/groovy";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import markdown from "highlight.js/lib/languages/markdown";
import powershell from "highlight.js/lib/languages/powershell";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import scss from "highlight.js/lib/languages/scss";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

const LANGUAGES = {
  bash,
  c,
  cpp,
  csharp,
  css,
  dockerfile,
  go,
  groovy,
  ini,
  java,
  javascript,
  json,
  kotlin,
  markdown,
  powershell,
  python,
  rust,
  scss,
  sql,
  typescript,
  xml,
  yaml,
} as const;

for (const [name, grammar] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, grammar);

const EXTENSION_LANGUAGE: Record<string, keyof typeof LANGUAGES> = {
  bash: "bash",
  c: "c",
  cc: "cpp",
  cfg: "ini",
  cjs: "javascript",
  conf: "ini",
  cpp: "cpp",
  cs: "csharp",
  css: "css",
  cxx: "cpp",
  env: "ini",
  go: "go",
  gradle: "groovy",
  groovy: "groovy",
  h: "c",
  hpp: "cpp",
  htm: "xml",
  html: "xml",
  hxx: "cpp",
  ini: "ini",
  java: "java",
  js: "javascript",
  json: "json",
  jsonl: "json",
  jsx: "javascript",
  kt: "kotlin",
  kts: "kotlin",
  md: "markdown",
  mdx: "markdown",
  mjs: "javascript",
  ps1: "powershell",
  psd1: "powershell",
  psm1: "powershell",
  py: "python",
  pyw: "python",
  rs: "rust",
  sass: "scss",
  scss: "scss",
  sh: "bash",
  sql: "sql",
  svelte: "xml",
  svg: "xml",
  toml: "ini",
  ts: "typescript",
  tsx: "typescript",
  vue: "xml",
  xml: "xml",
  yaml: "yaml",
  yml: "yaml",
  zsh: "bash",
};

/** Maps a changed file to a deterministic grammar; unknown files remain plain text. */
export function syntaxLanguageForPath(path: string): keyof typeof LANGUAGES | null {
  const basename = path.replace(/\\/g, "/").split("/").pop()?.toLocaleLowerCase() ?? "";
  if (basename === "dockerfile" || basename.startsWith("dockerfile.")) return "dockerfile";
  const extension = basename.includes(".") ? basename.slice(basename.lastIndexOf(".") + 1) : "";
  return EXTENSION_LANGUAGE[extension] ?? null;
}

/**
 * Highlight.js escapes source text before returning markup. Keeping this in a
 * single helper makes the deliberate innerHTML boundary easy to audit.
 */
export function highlightDiffLine(content: string, path: string) {
  return highlightSource(content, path);
}

/** Highlights a complete source document so multi-line grammar state is preserved. */
export function highlightSource(content: string, path: string) {
  const language = syntaxLanguageForPath(path);
  if (!language) return { html: escapeHtml(content), language: "plaintext" };
  try {
    return {
      html: hljs.highlight(content, { language, ignoreIllegals: true }).value,
      language,
    };
  } catch {
    return { html: escapeHtml(content), language: "plaintext" };
  }
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
