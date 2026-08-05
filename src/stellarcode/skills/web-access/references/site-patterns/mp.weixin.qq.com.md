---
domain: mp.weixin.qq.com
aliases: [微信公众号, weixin, 公众号文章]
---

# 微信公众号文章

- 静态 HTTP 抓取经常遇到反爬页或空正文，可直接用 Chrome DevTools MCP。
- 典型流程：`navigate_page` → 等正文出现 → `take_snapshot`。
- 标题常在 `#activity-name`，作者常在 `#js_name`，正文常在 `#js_content`。
- 图片多使用 `data-src` 懒加载；不要把占位图当原图。
- 页面显示“已被发布者删除”时直接报告不可访问，不重复刷新。
- 阅读单篇公开文章通常不需要 shared 登录态。

