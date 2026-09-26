# 工具并行实验

在仓库根目录运行，无 API Key、LLM、真实公网请求或浏览器操作：

```powershell
.\.venv\Scripts\python.exe experiments/benchmark_tool_parallelism.py
```

默认配置：固定种子 `20260920`；批次大小 2、4、8、16，各 25 批；
每批串行与并行各 3 次，共 600 次计时、4500 次顶层工具调用。
两组都使用生产代码 `ToolRegistry.execute_tools`，并发数分别为 1 和 4。
不经过 LLM、HITL 或桌面 Worktree 提交层，不修改生产代码。

先做快速验证：

```powershell
.\.venv\Scripts\python.exe experiments/benchmark_tool_parallelism.py --per-size 1 --repeats 1
.\.venv\Scripts\python.exe -m pytest experiments/test_tool_parallelism.py -q
```

## 覆盖边界

覆盖默认注册表的全部 11 个工具：`read_file`、`write_file`、`apply_patch`、
`delete_file`、`list_dir`、`glob_files`、`grep_code`、`execute_command`、
`web_search`、`web_fetch`、`search_code`；另覆盖 `load_skill` 和
`browser_status`、`browser_connect`、`browser_disconnect`、`browser_tabs`。
正式规模下每个工具至少在 10 个不同批次出现。注册表名单改变时断言失败，
必须先更新测试模板，不能静默遗漏新工具。

- 真实本地文件、子进程、搜索、Skill 实现，真实 RAG SQLite 检索。
- RAG 使用本地哈希 Embedding，预建 24 个文件、192 个函数的固定索引。
- Web 保留生产工具 handler、搜索 provider 解析和 HTML 提取；仅传输替换为固定
  响应，每次模拟等待 50ms。可用 `--web-delay-ms 0` 做敏感性分析。
- Browser 保留生产 controller，连接探针、MCP 重启和页面返回使用零延迟替身。
  **没有验证真正的 Chrome 启动、连接、MCP 协议吞吐。**
- 每批最多一个 Browser 操作，多个 Skill 调用加载同一测试 Skill，避免不确定的淘汰顺序。
- 读写目标互不依赖；修改只针对生成目录内的独立文件。没有测试同文件冲突或依赖图。
- 不包含动态 MCP 工具；嵌套页面查询替身不计作单独覆盖工具。

## 方法与结果

每次计时前恢复文件和可变状态；全部工具和线程调度先预热，预热不计入 600 次。
每个配对交替先串行/先并行；排除准备、索引构建、验证和结果落盘的时间。
调用使用固定有效参数，随机化的是工具组合、批次顺序和调用顺序，
并未随机化输入数据规模或模拟真实用户工具频率。

每个唯一结果目录包含 `manifest.json`、`runs.csv`、`tool_results.jsonl`、
`summary.json`、`report.md` 和生成的 `workspace/`。
原始结果与工作区默认不进入 Git。失败的试跑保留在自己的目录，不混入正式数据。
脚本只删除生成的 `delete_file` 测试文件，不清理项目文件或用户数据。

总体加速比 = 正确且结果等价配对的串行总耗时 / 并行总耗时；
耗时降低 = 1 - 并行总耗时 / 串行总耗时。失败次数另外报告，不能以失败换速度。
验证包括返回调用 ID 顺序、成功/超时标记、文件内容、完整 grep 命中集合，
以及串并行其他工具返回文本或副作用的指纹。

Web 的 50ms 是人为设定，不是测出来的公网延迟。总体指标只代表这份 manifest，
不能写成“所有 Agent 任务加速 X%”。报告同时列出不含 Web 的批次作为辅助观察，
但它们的组成不同，并不是严格的移除 Web 延迟因果对照。
