---
domain: juejin.cn
aliases: [掘金, Juejin]
---

# 掘金

- 文章详情通常有 SSR 正文，可先 `web_fetch`。
- 搜索和列表更偏 SPA，抓取不完整时使用浏览器。
- 浏览器流程：导航后等待 `#article-root`、`.article-content` 或 `article`，再 `take_snapshot`。
- 代码块要保留换行和缩进；必要时读取 `pre code` 的 `innerText`。
- 图片可能使用 `data-src` 懒加载。
- 付费小册仅能读取用户有权访问的内容。

