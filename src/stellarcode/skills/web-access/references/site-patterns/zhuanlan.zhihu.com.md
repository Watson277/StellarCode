---
domain: zhuanlan.zhihu.com
aliases: [知乎专栏, Zhihu]
---

# 知乎专栏

- 单篇专栏通常包含 SSR 正文，可先 `web_fetch` 一次。
- 正文为空、只有通用标题或登录墙时，切 Chrome DevTools MCP。
- 浏览器流程：`navigate_page` → 等 `.Post-RichTextContainer` 或 `article` → `take_snapshot`。
- 问答页与专栏不同，答案可能需要展开和滚动加载，不要把首屏当完整答案。
- 点赞、评论和关注需要登录；只有用户任务需要时才切 shared。

