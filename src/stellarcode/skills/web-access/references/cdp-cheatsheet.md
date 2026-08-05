# Chrome DevTools MCP 速查

具体工具名和参数以当前 `mcp__chrome-devtools__*` schema 为准，不要凭记忆构造参数。

## 常用流程

```text
读 SPA：navigate_page → wait_for → take_snapshot
填表：take_snapshot → fill / fill_form → click → wait_for → take_snapshot
调试：take_snapshot → list_console_messages → list_network_requests
多标签：list_pages → select_page → 操作
```

## 工具类别

- 导航：`navigate_page`、`new_page`、`list_pages`、`select_page`、`close_page`、`wait_for`
- 输入：`click`、`fill`、`fill_form`、`type_text`、`press_key`、`hover`、`drag`、`upload_file`、`handle_dialog`
- 读取与调试：`take_snapshot`、`take_screenshot`、`evaluate_script`、控制台和网络请求工具
- 性能：performance trace 和 Lighthouse 类工具

## 关键约束

1. DOM 阅读首选 `take_snapshot`；只有视觉任务才截图。
2. SPA 的 class 常变化，先从 snapshot 获取元素 uid，再操作。
3. 异步加载用 `wait_for`。
4. shared 模式只能关闭 StellarCode 自己新开的标签页。
5. 需要登录态时先 `browser_status`，再 `browser_connect`；完成后 `browser_disconnect`。

