---
domain: x.com
aliases: [Twitter, 推特]
---

# X / Twitter

- React SPA，静态抓取通常不足，优先 Chrome DevTools MCP。
- 读取单条公开内容：导航后等待 `article[data-testid=tweet]`，再 `take_snapshot`。
- 个人时间线、私信、列表和设置常需要 shared 登录态。
- 选择器优先使用 `data-testid`，不要依赖 hash class。
- 同一页面可能同时包含主帖和回复；必须根据上下文确认目标条目。
- 不绕过登录墙、速率限制或平台访问控制。

