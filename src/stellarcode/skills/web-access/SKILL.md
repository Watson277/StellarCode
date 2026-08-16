---
name: web-access
description: |
  所有联网与浏览器操作的决策手册：搜索、网页抓取、读取 SPA 或防爬站点、访问需要登录的页面，以及读取微信公众号、知乎、X、小红书和 GitHub。用户要求搜索信息、阅读网页、调研话题、查看登录后页面或操作动态站点时，先调用 load_skill 再选择工具链。
version: "1.0.0"
author: StellarCode Python
tags: [web, browser, fetch]
---

# web-access Skill

## 核心原则

围绕用户目标收集足够证据，拿到目标内容后停止。每一步都根据真实结果更新判断；同一工具失败后不要只换一种说法重复调用。

1. 明确成功标准：用户要事实、正文摘要、调研结论，还是页面操作结果。
2. 根据 URL、关键词、站点类型选择最直接的入口。
3. 检查状态码、搜索质量、正文完整度、登录墙和动态渲染迹象。
4. 需要事实核验时读取少量高质量来源，并在最终回答中保留 URL。

## 工具选择

| 场景 | 首选 | 失败后的下一步 |
|---|---|---|
| 只有关键词，需要找入口 | `web_search` | 从结果中选择高质量 URL，再 `web_fetch` |
| 已知普通公开 URL | `web_fetch` 一次 | 空正文、JS 壳或被拦截时改用 Chrome DevTools MCP |
| SPA、交互、控制台或网络调试 | Chrome DevTools MCP | `navigate_page` → `wait_for` → `take_snapshot` |
| 微信、X、小红书等静态抓取困难站点 | 直接使用 Chrome DevTools MCP | 根据页面证据决定是否需要 shared 模式 |
| GitHub 私仓、内部系统等登录后页面 | `browser_status` → `browser_connect` | 连接失败时报告精确引导，不猜测凭据 |
| 用户明确要求视觉检查或截图 | `take_screenshot` | 普通正文读取仍优先 `take_snapshot` |

已知 URL 不要先搜索。搜索结果摘要不足以支撑结论时，最多抓取三个最相关页面。不要把低质量搜索误判为“网上没有”。

## 浏览器模式

- isolated：默认临时浏览器，不含用户 Cookie，适合公开页面。
- shared：复用用户已登录的 Chrome，适合私仓、邮箱和内部系统。
- 先尝试获取目标；只有出现登录页、权限页或内容缺失时才切 shared。
- shared 模式下不要关闭用户原有标签页。完成登录态任务后调用 `browser_disconnect`。
- 浏览器阅读优先 `take_snapshot`；等待异步内容用 `wait_for`，不要盲目 sleep。

典型流程：

```text
navigate_page → wait_for → take_snapshot → 提取目标内容
                                  ↘ click / fill / fill_form
```

## References

加载结果会给出本 Skill 的 `references/` 绝对路径。确定站点后，可用 `list_dir` 检查 `site-patterns/`，命中域名时再用 `read_file` 读取对应文件：

- `cdp-cheatsheet.md`：Chrome DevTools MCP 常用工具与调试流程
- `site-patterns/github.com.md`
- `site-patterns/mp.weixin.qq.com.md`
- `site-patterns/zhuanlan.zhihu.com.md`
- `site-patterns/x.com.md`
- `site-patterns/xiaohongshu.com.md`
- `site-patterns/juejin.cn.md`

新经验应写入用户层 `~/.stellarcode/skills/web-access/references/site-patterns/<domain>.md`。默认模板首次启动时会安装到此目录，并且不会覆盖已有的用户版本；修改后执行 `/skill reload` 即可生效。

## 并发边界

多个互不依赖的 `web_search` / `web_fetch` 可以并发。浏览器通常只有一个活动会话，涉及同一页面状态的导航、等待、读取和点击必须保持顺序。

## 禁止事项

- 不在 SPA 页面反复调用 `web_fetch`。
- 不默认截图，不为“更全面”抓取大量低质量页面。
- 不代替用户输入密码或绕过登录、验证码、付费墙和访问控制。
- 不在用户未要求时切换 shared 浏览器或改动真实账户数据。

