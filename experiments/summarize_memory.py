"""Export completed or partial memory experiment data without making API calls."""
import csv
import json
from pathlib import Path
import sys


def main(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((root / "results.json").read_text(encoding="utf-8"))
    expected_runs = 18 if manifest['formal'] else 2
    calls = []
    for result in results:
        run = root / f"{result['task']}-{result['repeat']}-{result['mode']}"
        for line in (run / "calls.jsonl").read_text(encoding="utf-8").splitlines():
            calls.append({"task": result["task"], "repeat": result["repeat"],
                          "mode": result["mode"], **json.loads(line)})
    for name, rows in [("runs.csv", results), ("calls.csv", calls)]:
        keys = list(dict.fromkeys(k for row in rows for k in row))
        with (root / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
    lines = ["# 模型上下文压缩实验", "",
             f"阶段：{'正式' if manifest['formal'] else '双组试跑（不是正式18次实验）'}。",
             f"已记录 {len(results)}/{expected_runs} 次运行；"
             + ("全部运行已记录。" if len(results) == expected_runs else "当前为阶段性结果，不是最终结论。"),
             f"模型：`{manifest['model']}`；上下文声明容量 1M；实验组约 51.2K 估算 Token 触发压缩。",
             "两组使用真实 Agent 和 GLM，关闭长期记忆。固定12轮、6种受路径保护的文件工具。",
             "每轮通过 read_file 加载合成集成样本，模拟长上下文；不是自然用户任务分布。",
             f"温度0，单次输出上限8192，reasoning_effort={manifest.get('reasoning_effort', 'default')}；摘要与主任务都单独计量。",
             "", "| 任务 | 组 | 重复 | 轮数 | 功能+约束通过 | 约束通过 | 主任务平均输入 | 总输入（含摘要） | 总输出 | 压缩次数 | 耗时秒 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        lines.append(f"| {r['task']} | {r['mode']} | {r['repeat']} | {r['rounds_completed']} | "
                     f"{r['checks_passed']}/{r['checks_total']} | {r['constraints_passed']}/{r['constraints_total']} | "
                     f"{r['mean_main_prompt_tokens']:.0f} | {r['total_input_tokens']} | "
                     f"{r['total_output_tokens']} | {r['compactions']} | {r['elapsed_seconds']:.1f} |")
    pairs = {}
    for r in results:
        pairs.setdefault((r['task'], r['repeat']), {})[r['mode']] = r
    valid = [p for p in pairs.values() if set(p) == {'control', 'compact'}
             and all(r['passed'] and r['exact_usage'] for r in p.values())]
    if valid:
        control = sum(p['control']['total_input_tokens'] for p in valid)
        compact = sum(p['compact']['total_input_tokens'] for p in valid)
        lines += ["", f"双方验收通过且 usage 精确的配对数：{len(valid)}。",
                  f"这些配对计入摘要后总输入 Token 降低：{100*(1-compact/control):.2f}%。",
                  "这是双方成功样本上的条件统计，不代表全部任务，也不能据此证明压缩不影响成功率。"]
    else:
        lines += ["", "尚无双方验收通过且 usage 精确的完整配对，不能报告有效节省率。"]
    groups = {}
    for mode in ('control', 'compact'):
        runs = [r for r in results if r['mode'] == mode]
        count = sum(r['main_calls'] for r in runs)
        groups[mode] = {
            'runs': len(runs), 'passed': sum(r['passed'] for r in runs),
            'main_calls': count,
            'mean_main_prompt_tokens': sum(r['main_input_tokens'] for r in runs) / max(1, count),
            'total_input_tokens': sum(r['total_input_tokens'] for r in runs),
            'total_output_tokens': sum(r['total_output_tokens'] for r in runs),
            'cached_input_tokens': sum(r['cached_input_tokens'] for r in runs),
            'constraints_passed': sum(r['constraints_passed'] for r in runs),
            'constraints_total': sum(r['constraints_total'] for r in runs),
            'elapsed_seconds': sum(r['elapsed_seconds'] for r in runs),
            'compactions': sum(r['compactions'] for r in runs),
        }
    (root / 'aggregate.json').write_text(json.dumps(groups, indent=2), encoding='utf-8')
    lines += ['', '## 分组汇总（含失败运行，不将此表直接解释为节省率）', '',
              '| 组 | 任务成功 | 约束通过 | 主任务每请求平均输入 | 累计输入 | 累计缓存输入 | 累计输出 |',
              '|---|---:|---:|---:|---:|---:|---:|']
    for mode, g in groups.items():
        lines.append(f"| {mode} | {g['passed']}/{g['runs']} | "
                     f"{g['constraints_passed']}/{g['constraints_total']} | "
                     f"{g['mean_main_prompt_tokens']:.0f} | {g['total_input_tokens']} | "
                     f"{g['cached_input_tokens']} | {g['total_output_tokens']} |")
    turns = {}
    for call in calls:
        key = (call['task'], call['repeat'], call['mode'], call['turn'])
        row = turns.setdefault(key, dict(zip(('task', 'repeat', 'mode', 'turn'), key)))
        prefix = 'summary' if call['operation'] == 'history-compaction' else 'main'
        for metric in ('input_tokens', 'output_tokens'):
            label = f'{prefix}_{metric}'
            row[label] = row.get(label, 0) + call.get(metric, 0)
        row[f'{prefix}_calls'] = row.get(f'{prefix}_calls', 0) + 1
    with (root / 'turn_metrics.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        keys = list(dict.fromkeys(k for row in turns.values() for k in row))
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(turns.values())
    lines += ["", "## 限制", "",
              "- 失败或提前终止不能视为节省 Token。对照组真实超窗则不属于有效窗口内对照。",
              "- 压缩触发依据项目估算器；结果使用服务端实际 usage，二者可能不同。",
              "- 缓存命中会影响费用和耗时，不能直接按输入 Token 的降幅推断成本降幅。",
              "- 试跑用于验证触发、用量记录与验收，不作总体成功率结论。",
              "- 未计入单独的连通性预检请求；调用失败且无 usage 时消耗未知，不能按0解释。",
              "- 查看各 run 的 acceptance.json、events.jsonl 和 request/response 文件诊断失败。"]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(root / "report.md")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
