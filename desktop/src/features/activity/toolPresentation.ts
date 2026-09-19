import type { Translator } from "../../i18n";

export type ToolStatus = "waiting_approval" | "running" | "completed" | "failed";

export function compactText(value: string, limit = 140) {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text;
}

export function toolTarget(args?: Record<string, unknown>): string {
  if (!args) return "";
  for (const key of ["command", "path", "file_path", "filePath", "url", "query", "pattern", "directory", "city", "name"]) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (key === "command" && Array.isArray(value) && value.every((item) => typeof item === "string")) {
      return value.join(" ");
    }
  }
  return "";
}

export function toolFailure(name: string, detail: string, t: Translator) {
  // A Python source syntax error is not necessarily a shell command-format error.
  if (name === "execute_command" && /ParserError|MissingTypename|(?:bash|sh):[^\n]*syntax error/i.test(detail)) return {
    title: t("Command syntax error"), hint: t("The shell could not parse the command. Check its format before retrying."),
  };
  if (/SyntaxError|syntax error/i.test(detail)) return {
    title: t("Code syntax error"), hint: t("Check the file and line reported in the technical details."),
  };
  if (/timed?\s*out|TimeoutError/i.test(detail)) return {
    title: t("Operation timed out"), hint: t("Check the operation status before retrying; it may have made partial changes."),
  };
  if (/PermissionError|access is denied|permission denied|拒绝访问/i.test(detail)) return {
    title: t("Permission denied"), hint: t("Check the target permissions and current access mode."),
  };
  if (/FileNotFoundError|No such file or directory|cannot find the path|找不到指定的文件/i.test(detail)) return {
    title: t("File or directory not found"), hint: t("Check the path and current working directory."),
  };
  if (/CommandNotFoundException|command not found|not recognized as.*(?:command|cmdlet)|无法将.+识别为/i.test(detail)) return {
    title: t("Command not available"), hint: t("Check whether the program is installed and available in PATH."),
  };
  if (/Too Many Requests|(?:HTTP(?:StatusError)?[^\n]{0,50}|status(?:_code)?[\s:=]+|error[\s:]+)429\b/i.test(detail)) return {
    title: t("Service rate limit reached"), hint: t("Wait before retrying or check your service quota."),
  };
  if (/ENOTFOUND|NameResolutionError|ConnectError|connection refused|name resolution|无法解析/i.test(detail)) return {
    title: t("Could not connect to the service"), hint: t("Check the network, service address, and proxy settings."),
  };
  const code = detail.match(/^exit_code:\s*(-?\d+)\b/m)?.[1];
  return {
    title: name === "execute_command" && code && code !== "0"
      ? t("Command exited with code {code}", { code }) : t("Tool execution failed"),
    hint: t("Review the output before deciding whether to retry."),
  };
}

export function toolResultSummary(status: ToolStatus, detail: string, t: Translator): string {
  if (status === "running") return t("Operation in progress");
  if (status === "waiting_approval") return t("Waiting for your decision");
  if (!detail.trim()) return t("Completed with no output");
  // Read structured output when available; never dump a whole JSON object into the headline.
  try {
    const result: unknown = JSON.parse(detail);
    if (Array.isArray(result)) return t("Returned {count} items", { count: result.length });
    if (typeof result === "string") return compactText(result);
    if (result && typeof result === "object") {
      const object = result as Record<string, unknown>;
      for (const key of ["summary", "message", "stdout"]) {
        if (typeof object[key] === "string" && object[key].trim()) return compactText(object[key]);
      }
      if (Array.isArray(object.content)) {
        const text = object.content.find((item) => item && item.type === "text" && typeof item.text === "string" && item.text.trim());
        if (text) return compactText(text.text);
      }
      for (const key of ["results", "items", "files", "entries"]) {
        if (Array.isArray(object[key])) return t("Returned {count} items", { count: object[key].length });
      }
      return t("Returned structured data");
    }
  } catch { /* Plain text and truncated previews remain valid tool output. */ }
  const lines = detail.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const preview = lines.find((line) => !/^(?:exit_code:\s*-?\d+|stdout:|stderr:)$/i.test(line));
  if (preview) return compactText(preview);
  return /^exit_code:\s*0\b/m.test(detail) ? t("Command completed with no output") : t("Completed with no output");
}

export function toolDuration(elapsed: string) {
  const milliseconds = elapsed.match(/^(\d+(?:\.\d+)?)\s*ms$/)?.[1];
  if (!milliseconds) return elapsed;
  const value = Number(milliseconds);
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`;
}
