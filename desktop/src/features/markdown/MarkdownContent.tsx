import { useEffect, useRef, useState, type ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Translator } from "../../i18n";
import {
  codeLanguageFromClassName,
  codeLanguageLabel,
  isDiffLanguage,
  resolveWorkspaceFileReference,
  type WorkspaceFileReference,
} from "./codeAnswer";

interface MarkdownContentProps {
  content: string;
  workspace?: string;
  projectId?: string;
  t: Translator;
  reviewChangesBusy?: boolean;
  onReviewChanges?: () => void;
  onOpenWorkspaceFile?: (reference: WorkspaceFileReference) => void | Promise<void>;
}

/** Shared answer renderer. Syntax highlighting can be added inside CodeBlock without changing transcript state. */
export function MarkdownContent({
  content,
  workspace = "",
  projectId = "",
  t,
  reviewChangesBusy = false,
  onReviewChanges,
  onOpenWorkspaceFile,
}: MarkdownContentProps) {
  const [actionError, setActionError] = useState("");

  async function openWorkspaceFile(rawPath: string) {
    const reference = resolveWorkspaceFileReference(rawPath, workspace);
    if (!reference || !projectId) return;
    setActionError("");
    try {
      if (onOpenWorkspaceFile) {
        await onOpenWorkspaceFile(reference);
      } else {
        await invoke<string>("workspace_file_open", {
          projectId,
          relativePath: reference.relativePath,
        });
      }
    } catch (error) {
      setActionError(t("Open file failed: {error}", { error: String(error) }));
    }
  }

  function renderFileReference(rawPath: string, children: ReactNode) {
    const reference = resolveWorkspaceFileReference(rawPath, workspace);
    if (!reference || !projectId) return null;
    const location = reference.line
      ? `:${reference.line}${reference.column ? `:${reference.column}` : ""}`
      : "";
    return <button
      type="button"
      className="markdown-file-reference"
      onClick={() => void openWorkspaceFile(rawPath)}
      title={t("Open {path}", { path: `${reference.relativePath}${location}` })}
    >{children}</button>;
  }

  return <div className="markdown-content">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ children, href = "", node: _node, ...props }) => {
          const fileReference = renderFileReference(href, children);
          return fileReference ?? <a {...props} href={href} target="_blank" rel="noreferrer">{children}</a>;
        },
        code: ({ children, className, node: _node, ...props }) => {
          const value = String(children).replace(/\n$/, "");
          const fileReference = !className
            ? renderFileReference(value, <code {...props}>{children}</code>)
            : null;
          return fileReference ?? <code {...props} className={className}>{children}</code>;
        },
        pre: ({ node, children }) => {
          const block = readCodeBlock(node);
          if (!block) return <pre>{children}</pre>;
          return <CodeBlock
            code={block.code}
            language={block.language}
            t={t}
            reviewChangesBusy={reviewChangesBusy}
            onReviewChanges={onReviewChanges}
          />;
        },
      }}
    >
      {content}
    </ReactMarkdown>
    {actionError && <div className="markdown-action-error" role="status">{actionError}</div>}
  </div>;
}

function CodeBlock({
  code,
  language,
  t,
  reviewChangesBusy,
  onReviewChanges,
}: {
  code: string;
  language: string;
  t: Translator;
  reviewChangesBusy: boolean;
  onReviewChanges?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const timerRef = useRef<number | null>(null);
  const diff = isDiffLanguage(language);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  async function copyCode() {
    setCopyError("");
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => setCopied(false), 1_500);
    } catch (error) {
      setCopyError(t("Copy failed: {error}", { error: String(error) }));
    }
  }

  return <figure className={`markdown-code-block language-${language}`} data-language={language}>
    <figcaption>
      <span>{t(codeLanguageLabel(language))}</span>
      <div>
        {diff && <button
          type="button"
          onClick={onReviewChanges}
          disabled={!onReviewChanges || reviewChangesBusy}
          title={onReviewChanges ? t("Open this task in Review Changes") : t("No protected task changes are available for this answer.")}
        >{t(reviewChangesBusy ? "Loading diff..." : "Open Review Changes")}</button>}
        <button type="button" onClick={() => void copyCode()}>{t(copied ? "Copied" : "Copy code")}</button>
      </div>
    </figcaption>
    <pre><code className={`language-${language}`}>{code || " "}</code></pre>
    {copyError && <small className="markdown-code-error" role="status">{copyError}</small>}
  </figure>;
}

type HastLikeNode = {
  type?: string;
  value?: string;
  tagName?: string;
  properties?: { className?: unknown };
  children?: HastLikeNode[];
};

function readCodeBlock(node: unknown) {
  const pre = node as HastLikeNode | undefined;
  const codeNode = pre?.children?.find((child) => child.type === "element" && child.tagName === "code");
  if (!codeNode) return null;
  const classNames = Array.isArray(codeNode.properties?.className)
    ? codeNode.properties?.className.map(String).join(" ")
    : String(codeNode.properties?.className ?? "");
  return {
    language: codeLanguageFromClassName(classNames),
    code: textContent(codeNode).replace(/\n$/, ""),
  };
}

function textContent(node: HastLikeNode): string {
  if (node.type === "text") return node.value ?? "";
  return node.children?.map(textContent).join("") ?? "";
}
