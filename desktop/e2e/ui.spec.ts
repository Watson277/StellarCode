import { test, expect, type Page } from "@playwright/test";

async function ready(page: Page) {
  page.on("pageerror", (error) => { throw error; });
  await page.goto("/e2e/index.html");
  await expect(page.locator(".connection-state")).toContainText("Runtime ready");
}
async function settings(page: Page) {
  await page.getByRole("button", { name: "Open settings", exact: true }).click();
  return page.getByRole("dialog", { name: "Settings & Management" });
}
async function event(page: Page, type: string, data: unknown) {
  await page.evaluate(({ type, data }) => (window as any).fixture.event(type, data), { type, data });
}

test("5000-message history mounts recent rows and preserves the reading anchor", async ({ page }) => {
  await page.goto("/e2e/index.html?history=5000");
  await expect(page.locator(".connection-state")).toContainText("Runtime ready");
  await expect(page.locator(".transcript .message")).toHaveCount(80);
  await expect(page.locator(".transcript")).toContainText("History message 4999");
  const transcript = page.locator(".transcript");
  await transcript.evaluate((element) => { element.scrollTop = 0; });
  const oldFirst = transcript.locator(".message").first();
  const before = await oldFirst.boundingBox();
  await transcript.getByRole("button", { name: /Show earlier messages/ }).click();
  await expect(transcript.locator(".message")).toHaveCount(160);
  const oldAnchor = transcript.locator(".message").filter({ hasText: "History message 4920" });
  const after = await oldAnchor.boundingBox();
  expect(Math.abs(after!.y - before!.y)).toBeLessThan(3);
  await page.screenshot({ path: "test-results/long-history.png", fullPage: true });
  await page.getByRole("button", { name: "Conversation 2", exact: true }).click();
  await expect(transcript).not.toContainText("History message");
  await page.getByRole("button", { name: /Conversation 1/ }).first().click();
  await expect(transcript.locator(".message")).toHaveCount(80);
});

test("stream batches keep final text, reset boundaries and conversation isolation", async ({ page }) => {
  await ready(page);
  await page.locator(".composer textarea").fill("Stream a response");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  await page.evaluate(async () => {
    for (let index = 0; index < 100; index++) await (window as any).fixture.event("assistant.delta", { text: `part${index} ` });
    await (window as any).fixture.event("assistant.completed", { content: "Final complete answer" });
  });
  await expect(page.locator(".transcript .markdown-content")).toHaveText("Final complete answer");
  await event(page, "assistant.delta", { text: "draft" });
  await event(page, "assistant.delta", { text: "", reset: true });
  await event(page, "assistant.delta", { text: "replacement" });
  await expect(page.locator(".transcript .markdown-content").last()).toHaveText("replacement");
  await page.getByRole("button", { name: "Conversation 2", exact: true }).click();
  await page.evaluate(() => (window as any).fixture.event("assistant.delta", { text: "Late old-session text" }, "conversation-1"));
  await page.waitForTimeout(80);
  await expect(page.locator(".transcript")).not.toContainText("Late old-session text");
  await expect(page.locator(".transcript")).not.toContainText("replacement");
});

test("streaming does not steal scroll and completed Markdown keeps its local state", async ({ page }) => {
  await page.goto("/e2e/index.html?history=200");
  await expect(page.locator(".connection-state")).toContainText("Runtime ready");
  await page.locator(".composer textarea").fill("Show code");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  const transcript = page.locator(".transcript");
  await transcript.evaluate((element) => { element.scrollTop = 600; });
  await page.waitForTimeout(60);
  const before = await transcript.evaluate((element) => element.scrollTop);
  await event(page, "assistant.delta", { text: "new output" });
  await expect(transcript).toContainText("new output");
  expect(await transcript.evaluate((element) => element.scrollTop)).toBe(before);
  await event(page, "assistant.completed", { content: "```python\nprint('hello')\n```" });
  await page.locator(".transcript-jump").click();
  await event(page, "task.completed", { status: "completed", elapsed_ms: 200 });
  await expect(page.locator(".composer textarea")).toBeEnabled();
  await page.evaluate(() => Object.defineProperty(navigator.clipboard, "writeText", { configurable: true, value: () => Promise.resolve() }));
  await transcript.getByRole("button", { name: "Copy code", exact: true }).click();
  await page.locator(".composer textarea").fill("Editing a new draft");
  await expect(transcript.getByRole("button", { name: "Copied", exact: true })).toBeVisible();
});

test("settings and Markdown modules are loaded only when needed", async ({ page }) => {
  const modules: string[] = [];
  page.on("request", (request) => modules.push(request.url()));
  await ready(page);
  expect(modules.some((url) => /\/src\/SettingsPage\.tsx/.test(url))).toBe(false);
  expect(modules.some((url) => /\/src\/features\/markdown\/MarkdownContent\.tsx/.test(url))).toBe(false);
  await settings(page);
  expect(modules.some((url) => /\/src\/SettingsPage\.tsx/.test(url))).toBe(true);
});

test("settings traps focus, confirms unsaved changes and restores the trigger", async ({ page }) => {
  await ready(page);
  const dialog = await settings(page);
  const close = dialog.getByRole("button", { name: "Close settings" });
  await expect(close).toBeFocused();
  await close.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Save Settings", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(close).toBeFocused();
  await dialog.getByRole("combobox", { name: "Send message" }).selectOption("ctrl-enter");
  await page.evaluate(() => { (window as any).fixture.confirm = false; });
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).fixture.calls.filter((c: any) => c.command === "plugin:dialog|message").length)).toBe(1);
  await page.evaluate(() => { (window as any).fixture.confirm = true; });
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Open settings", exact: true })).toBeFocused();
});

test("settings surfaces save errors and retains the draft for retry", async ({ page }) => {
  await ready(page);
  const dialog = await settings(page);
  const shortcut = dialog.getByRole("combobox", { name: "Send message" });
  await shortcut.selectOption("ctrl-enter");
  await page.evaluate(() => { (window as any).fixture.failSave = true; });
  await dialog.getByRole("button", { name: "Save Settings", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("read-only");
  await expect(shortcut).toHaveValue("ctrl-enter");
  await page.evaluate(() => { (window as any).fixture.failSave = false; });
  await dialog.getByRole("button", { name: "Save Settings", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("Settings saved");
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await settings(page);
  await expect(shortcut).toHaveValue("ctrl-enter");
});

test("send and stop use task lifecycle; conversation drafts remain isolated", async ({ page }) => {
  await ready(page);
  const input = page.locator(".composer textarea");
  await input.fill("Inspect this project");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  const stop = page.getByRole("button", { name: "Stop", exact: true });
  await expect(stop).toBeEnabled();
  await expect(page.locator(".user-message-text")).toContainText("Inspect this project");
  await stop.click();
  await expect(stop).toBeDisabled();
  await input.fill("Unsent draft");
  await page.getByRole("button", { name: "Conversation 2", exact: true }).click();
  await expect(input).toHaveValue("");
  await page.getByRole("button", { name: /Conversation 1/ }).first().click();
  await expect(input).toHaveValue("Unsent draft");
});

test("approval remains actionable and failure explains syntax before raw output", async ({ page }) => {
  await ready(page);
  await page.locator(".composer textarea").fill("Check Git");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  await event(page, "approval.requested", { approval_id: "approval-1", tool_call_id: "tool-1", name: "execute_command", arguments: { command: "git status" }, danger_level: "high", risk_description: "Run a command" });
  await page.getByRole("button", { name: "Allow once", exact: true }).click();
  await expect(page.locator(".approval-card.pending")).toHaveCount(0);
  await event(page, "tool.started", { tool_call_id: "tool-1", name: "execute_command", arguments: { command: "git status" }, iteration: 1 });
  await event(page, "tool.failed", { tool_call_id: "tool-1", name: "execute_command", error: "ParserError: MissingTypename\n位置 行:1 字符:2", elapsed_ms: 1422, timed_out: false });
  const card = page.locator(".tool-failed").first();
  await expect(card.locator(".tool-target")).toHaveText("git status");
  await expect(card.locator(".tool-duration")).toHaveText("1.42 s");
  await expect(card.locator(".tool-failure-summary")).toContainText("Command syntax error");
  await expect(card.locator(".tool-raw-output pre")).not.toBeVisible();
  await card.getByText("Technical details", { exact: true }).click();
  await expect(card.locator(".tool-raw-output pre")).toContainText("位置 行:1 字符:2");
});

test("tool summaries retain the operation target after completion and replay", async ({ page }) => {
  await ready(page);
  await page.locator(".composer textarea").fill("Search documentation");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  await event(page, "tool.started", { tool_call_id: "search-1", name: "web_search", arguments: { query: "Python async", top_k: 2 }, iteration: 1 });
  await event(page, "tool.completed", { tool_call_id: "search-1", name: "web_search", result_preview: '{"results":[{"title":"A"},{"title":"B"}]}', elapsed_ms: 920, success: true, has_attachments: false });
  const card = page.locator(".tool-completed").first();
  await expect(card.locator(".tool-target")).toHaveText("Python async");
  await expect(card.locator(".tool-detail-preview")).toHaveText("Returned 2 items");
  await expect(card.locator(".tool-duration")).toHaveText("920 ms");
  await expect(card).not.toHaveAttribute("open", "");
  await event(page, "task.completed", { status: "completed", elapsed_ms: 1000 });
  await expect(page.locator(".composer textarea")).toBeEnabled();
  await expect(card.locator(".tool-target")).toHaveText("Python async");
  await card.locator(":scope > summary").click();
  await card.getByText("Operation parameters", { exact: true }).click();
  await expect(card.locator(".tool-arguments pre")).toContainText('"top_k": 2');
});

test("Chinese tool cards fit a narrow sidebar without exposing raw errors", async ({ page }) => {
  await page.setViewportSize({ width: 760, height: 780 });
  await page.goto("/e2e/index.html?cards&lang=zh");
  await expect(page.locator(".tool-failure-summary")).toContainText("命令语法错误");
  await expect(page.locator(".tool-raw-output pre")).not.toBeVisible();
  const overflow = await page.locator(".tool-card").evaluateAll((cards) => cards.some((card) => card.scrollWidth > card.clientWidth + 1));
  expect(overflow).toBe(false);
  const overlap = await page.locator(".tool-card-header").evaluateAll((headers) => headers.some((header) => {
    const summary = header.querySelector(".tool-card-summary")!.getBoundingClientRect();
    const status = header.querySelector(".tool-result")!.getBoundingClientRect();
    return summary.right > status.left + 1 && summary.left < status.right - 1 && summary.bottom > status.top + 1 && summary.top < status.bottom - 1;
  }));
  expect(overlap).toBe(false);
  await page.screenshot({ path: "test-results/tool-cards-chinese.png", fullPage: true });
});

test("tool details preserve collapse; clipboard failure is visible; render recovery is local", async ({ page }) => {
  await page.goto("/e2e/index.html?components");
  await page.getByRole("button", { name: "Fail operation" }).click();
  const card = page.locator(".tool-failed");
  await expect(card).toHaveAttribute("open", "");
  await card.locator(":scope > summary").click();
  await expect(card).not.toHaveAttribute("open", "");
  await card.locator(":scope > summary").click();
  await page.evaluate(() => Object.defineProperty(navigator.clipboard, "writeText", { configurable: true, value: () => Promise.reject(new Error("Denied")) }));
  await card.getByRole("button", { name: "Copy details" }).click();
  await expect(card.getByRole("status")).toContainText("Copy failed");
  await page.getByRole("button", { name: "Repair fixture" }).click();
  await page.getByRole("button", { name: "Retry display" }).click();
  await expect(page.getByText("Recovered view")).toBeVisible();
  await expect(card).toBeVisible();
  await page.screenshot({ path: "test-results/tool-recovery.png", fullPage: true });
});

test("Runtime reconnects after a process exit", async ({ page }) => {
  await ready(page);
  await page.evaluate(() => (window as any).fixture.disconnect());
  await expect.poll(() => page.evaluate(() => (window as any).fixture.calls.filter((c: any) => c.command === "runtime_start").length)).toBe(2);
  await expect(page.locator(".connection-state")).toContainText("Runtime ready");
  await page.locator(".composer textarea").fill("Continue after reconnection");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
});

test("completed changes open in the reviewer and undo waits for confirmation", async ({ page }) => {
  await ready(page);
  await page.locator(".composer textarea").fill("Update example.py");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  await page.evaluate(() => (window as any).fixture.completeWithChanges());
  await page.locator(".task-change-files button").click();
  await expect(page.getByRole("region", { name: "Review changes" })).toBeVisible();
  await expect(page.locator(".review-workbench")).toContainText("value = 2");
  await page.evaluate(() => { (window as any).fixture.confirm = false; });
  await page.locator(".task-change-actions").getByRole("button", { name: "Undo task changes" }).click();
  expect(await page.evaluate(() => (window as any).fixture.calls.filter((c: any) => c.args?.message?.method === "task.rollback").length)).toBe(0);
  await page.evaluate(() => { (window as any).fixture.confirm = true; });
  await page.locator(".task-change-actions").getByRole("button", { name: "Undo task changes" }).click();
  await expect.poll(() => page.evaluate(() => (window as any).fixture.calls.filter((c: any) => c.args?.message?.method === "task.rollback").length)).toBe(1);
  await expect(page.locator(".task-change-actions button")).toBeDisabled();
});

test("Chinese labels and narrow settings stay readable", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 650 });
  await page.goto("/e2e/index.html?lang=zh");
  await page.getByRole("button", { name: "打开设置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "保存设置", exact: true })).toBeInViewport();
  const overlap = await dialog.locator(".settings-row").evaluateAll((rows) => rows.some((row) => {
    const label = row.children[0].getBoundingClientRect();
    const control = row.children[1].getBoundingClientRect();
    return label.right > control.left + 1 && label.bottom > control.top + 1 && control.bottom > label.top + 1;
  }));
  expect(overlap).toBe(false);
  await page.screenshot({ path: "test-results/settings-chinese.png", fullPage: true });
});

for (const [width, height] of [[1380, 860], [760, 600]]) {
  test(`layout and settings fit ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await ready(page);
    const dialog = await settings(page);
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Save Settings", exact: true })).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/settings-${width}.png`, fullPage: true });
    await dialog.getByRole("button", { name: "Appearance", exact: true }).click();
    await page.getByText("Light", { exact: true }).click();
    await dialog.getByRole("button", { name: "Save Settings", exact: true }).click();
    await expect(page.locator(".app-shell")).toHaveClass(/theme-light/);
    await page.screenshot({ path: `test-results/settings-light-${width}.png`, fullPage: true });
  });
}
