# 窗口内上下文压缩实验

读取仓库 `.env` 的 `LLM_MODEL_NAME / LLM_BASE_URL / LLM_API_KEY`，真实调用 GLM。
会消耗 API 额度，默认只运行双组试跑，不自动启动正式 18 次。

当前预检（2026-09-20）：`glm-5.3-flash` 拒绝 `thinking.type=disabled`，
返回 HTTP 400 / 1210；支持 `reasoning_effort=low/high/max`，不传默认 max。
low 仍然产生思考 Token，不能称为关闭思考。首轮默认max的试跑已按用户要求中止。
用户已确认两组统一使用low，当前脚本将主任务与摘要请求都固定为low，重新试跑后执行18次。

```powershell
# 一次性串行执行：新的low双组试跑 → 门槛检查 → 18次正式运行 → CSV/报告
# 会持续消耗API额度；已有campaign运行时不要重复启动。
.\.venv\Scripts\python.exe experiments/run_memory_campaign.py

# 配置加载器，12轮 × 2组，真实Agent运行
.\.venv\Scripts\python.exe experiments/benchmark_memory.py

# 先检查试跑结果、触发次数和验收，再执行正式规模
.\.venv\Scripts\python.exe experiments/benchmark_memory.py --formal

# 离线检查，无 API 请求
.\.venv\Scripts\python.exe -m pytest experiments/test_memory_experiment.py -q

# 指定某次结果目录，导出 CSV 和 Markdown（不调用模型）
.\.venv\Scripts\python.exe experiments/summarize_memory.py experiments/results/memory-YYYYMMDD-HHMMSS
```

## 固定设置

- 用户声明的模型窗口为 1,000,000 Token，两个实验组保持相同。
- 对照组只禁用压缩，不裁剪历史；实验组覆盖触发比值，使阈值为 51,200 **估算** Token。
- 使用生产 `Agent`、`ConversationHistoryCompactor`、模型客户端、流式解析器。
- 客户端实验子类添加每次输出上限 8192 Token；温度0；两组主任务和摘要统一 reasoning_effort=low。
- 保留最近3轮；使用生产压缩策略，包括生产实现的工具结果截断/摘要回退。
  不把仅发生工具截断当成“LLM 摘要压缩成功”。
- 长期记忆提取、注入关闭；没有 Skill、MCP、Web 或 shell 工具。
- 复用现有 benchmark 路径保护：只能读取独立 workspace，只能修改 solution.py。
- 私有验收由父脚本执行，不向Agent提供测试文件或动态反馈。
- 每轮最多8轮工具迭代；失败调用不重试；每次调用记录真实 usage，缺失时明确标记估算。
- 请求估算超过700K、每run超过120次记录或累计输入超过800万时触发安全上限。
- 每次实验使用新目录，保留输入/响应/事件/各轮代码快照；不会操作用户项目和记忆。

## 工作负载

配置加载器、CSV处理、任务管理存储三个标准库模块，每个12轮增量功能。
前期提出持久约束，后期通过私有测试验证。每轮要求读取150条不同的合成集成记录，
通过真实 `read_file` 返回积累长上下文。它是可复现的受控负载，**不是自然用户任务样本**。
无压缩时仅语料的项目估算量在80K～200K间；实际量以试跑的API usage为准。

正式规模：3任务 × 3重复 × 2组 = 18次完整运行。
两组交替先后顺序，不并发请求以减轻限流干扰；试跑数据与正式数据分目录保存。

## 试跑门槛

检查 `pilot-gate.json`：两组均完成12轮、实验组至少一次LLM摘要压缩、
对照组远低于真实窗口、usage精确。验收失败需要先定位：若是测试错误应修测试并重新冻结；
若是真实模型错误，应保留为失败，不为提高成绩放宽验收。

campaign自动门槛检查不要求任务成功率为100%：功能/约束失败是需要保留的实验结果，
不能筛掉失败后才做正式测量。协议/usage错误、未发生LLM压缩或对照超窗则停止。
`campaign.json` 记录总阶段，正式run逐个写入 `formal/results.json`；结束后生成
`formal/report.md`、`formal/runs.csv`、`formal/calls.csv` 和 `formal/turn_metrics.csv`。

运行结果在 `results.json`，每个run包含 `calls.jsonl`、`events.jsonl`、
`acceptance.json`、完整请求/响应、每轮solution快照和最终消息历史。
完整请求只含合成任务数据，不包含API Key；仍建议将整个results目录保留为本地数据。

## 解读限制

单次输入均值、总输入（含摘要）、输出、缓存命中、压缩耗时、任务成功和约束保留分别记录。
只在双方成功的配对上解释有效Token节省，失败导致的提前终止不算优化。
计费不能只按输入Token同比推导；GLM默认思考也可能使输出和延迟变化。
这测量上下文压缩，不测量长期记忆提取、作用域判断或跨会话复用。
