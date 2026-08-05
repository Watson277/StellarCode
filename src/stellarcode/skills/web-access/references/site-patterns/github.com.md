---
domain: github.com
aliases: [GitHub, GH]
---

# GitHub

- 公开 README 和普通文件可先用 `web_fetch`，raw URL 通常更干净。
- 私仓、私有 Issues/PR、Settings 和 Actions 必须使用 shared 浏览器。
- GitHub 未授权 API 对私仓常返回 404，而不是明确的 401/403；不要据此断言仓库不存在。
- 仓库目录和长讨论可能动态加载，静态正文不完整时切浏览器并 `take_snapshot`。
- 默认分支不一定是 `main`，构造 raw URL 前先从页面或 API 确认。
- `/settings`、`/security`、`/billing`、`/admin` 属于敏感区域，未经用户明确要求不要改写。

