import { useEffect, useMemo, useRef, useState } from "react";
import type { Translator } from "../../i18n";
import { highlightSource, syntaxLanguageForPath } from "../review/syntaxHighlight";
import "./FilePreviewPanel.css";

export interface WorkspaceFilePreview {
  relative_path: string;
  absolute_path: string;
  file_name: string;
  content: string;
  size_bytes: number;
  truncated: boolean;
  line?: number;
  column?: number;
}

interface FilePreviewPanelProps {
  preview: WorkspaceFilePreview;
  t: Translator;
}

/** Read-only source viewer for file references opened from Agent answers. */
export function FilePreviewPanel({ preview, t }: FilePreviewPanelProps) {
  const [copied, setCopied] = useState<"path" | "content" | null>(null);
  const scrollRef = useRef<HTMLPreElement | null>(null);
  const language = syntaxLanguageForPath(preview.relative_path) ?? "text";
  const highlighted = useMemo(
    () => highlightSource(preview.content || " ", preview.relative_path),
    [preview.content, preview.relative_path],
  );

  useEffect(() => {
    if (!preview.line || !scrollRef.current) return;
    // The viewer uses a stable 1.6 line-height; this positions referenced lines
    // without splitting highlighted multi-line spans into invalid markup.
    scrollRef.current.scrollTop = Math.max(0, (preview.line - 3) * 16);
  }, [preview.line, preview.relative_path]);

  async function copy(target: "path" | "content") {
    try {
      await navigator.clipboard.writeText(target === "path" ? preview.absolute_path : preview.content);
      setCopied(target);
      window.setTimeout(() => setCopied(null), 1_500);
    } catch {
      setCopied(null);
    }
  }

  return <section className="file-preview-panel">
    <header className="file-preview-titlebar">
      <div><span className="file-preview-eyebrow">{t("File preview")}</span><strong>{preview.file_name}</strong></div>
      <div><span className="file-preview-language">{language}</span></div>
    </header>
    <div className="file-preview-pathbar">
      <div><span>{t("Absolute path")}</span><code title={preview.absolute_path}>{preview.absolute_path}</code></div>
      <button type="button" onClick={() => void copy("path")}>{t(copied === "path" ? "Copied" : "Copy path")}</button>
    </div>
    <div className="file-preview-meta">
      <span>{formatBytes(preview.size_bytes)}</span>
      {preview.line && <span>{t("Line {line}", { line: preview.line })}{preview.column ? `:${preview.column}` : ""}</span>}
      {preview.truncated && <strong>{t("Preview truncated at 2 MB")}</strong>}
      <button type="button" onClick={() => void copy("content")}>{t(copied === "content" ? "Copied" : "Copy content")}</button>
    </div>
    <pre className="file-preview-source" ref={scrollRef}><code
      className={`file-preview-code language-${highlighted.language}`}
      // highlightSource emits escaped source plus markup created by Highlight.js.
      dangerouslySetInnerHTML={{ __html: highlighted.html }}
    /></pre>
  </section>;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
