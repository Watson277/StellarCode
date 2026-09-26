"""Offline paired benchmark through the production execute_tools scheduler.

No LLM, real browser, credentials, or outbound network. All writes/deletes target
generated fixtures beneath the unique results directory. External dependencies
are explicitly simulated, NOT measured as real provider/browser performance.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx

from stellarcode.browser.connectivity import BrowserProbe
from stellarcode.browser.controller import BrowserController, register_browser_tools
from stellarcode.browser.session import BrowserSession
from stellarcode.mcp.manager import McpServerStatus
from stellarcode.rag.embedding import EmbeddingClient
from stellarcode.rag.service import RagService
from stellarcode.skill.context import SkillContextBuffer
from stellarcode.skill.registry import SkillRegistry
from stellarcode.skill.tools import register_skill_tools
from stellarcode.tools.builtin import build_default_registry
from stellarcode.tools.registry import ToolDefinition, ToolInvocation, ToolRegistry
from stellarcode.web import NetworkPolicy, WebFetcher, ZhipuSearchProvider


BROWSER = ["browser_status", "browser_connect", "browser_disconnect", "browser_tabs"]
BASE = ["read_file", "write_file", "apply_patch", "delete_file", "list_dir",
        "glob_files", "grep_code", "execute_command", "web_search", "web_fetch",
        "search_code", "load_skill"]
ALL = sorted(BASE + BROWSER)
SOURCE = "".join(
    f"def fn_{i}(value):\n    # fixture_token\n    return value + {i}\n\n"
    for i in range(8)
)
WEB_MARKER = "FixtureWebMarker"


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class FixtureBrowserManager:
    """Dependency double: never launches npx or connects to a user's Chrome."""
    def __init__(self):
        self.item = SimpleNamespace(status=McpServerStatus.READY, tools=[],
                                    config=SimpleNamespace(args=[]), error_message="")

    def server(self, name):
        assert name == "chrome-devtools"
        return self.item

    def restart_with_args(self, name, args):
        assert name == "chrome-devtools"
        self.item.config.args = list(args)
        return "fixture_browser_restart_ok"


class FixtureProbe:
    def probe(self, port):
        return BrowserProbe(True, f"http://127.0.0.1:{port}", "fixture Chrome")


def generate_manifest(seed, per_size):
    rng = random.Random(seed)
    batches = [{"id": f"b{size}-{i:02d}", "size": size, "calls": []}
               for size in (2, 4, 8, 16) for i in range(per_size)]
    # At most one browser operation per batch: these mutate shared session state.
    # Put each browser tool in >=10 distinct batches for the full experiment.
    slots = list(range(len(batches)))
    rng.shuffle(slots)
    browser_count = min(len(batches), 48)
    for i, index in enumerate(slots[:browser_count]):
        batches[index]["calls"].append({"name": BROWSER[i % 4]})
    remaining = sum(b["size"] - len(b["calls"]) for b in batches)
    pool = (BASE * ((remaining + len(BASE) - 1) // len(BASE)))[:remaining]
    rng.shuffle(pool)
    for batch in batches:
        while len(batch["calls"]) < batch["size"]:
            batch["calls"].append({"name": pool.pop()})
        rng.shuffle(batch["calls"])
        for i, call in enumerate(batch["calls"]):
            call["id"] = f"{batch['id']}-c{i:02d}"
    rng.shuffle(batches)
    coverage = Counter(n for b in batches for n in {c["name"] for c in b["calls"]})
    if per_size == 25:
        assert set(coverage) == set(ALL) and min(coverage.values()) >= 10, coverage
    return batches


def initialize(root, web_delay_ms):
    corpus = root / "corpus"
    corpus.mkdir(parents=True)
    for i in range(24):
        (corpus / f"module_{i:02d}.py").write_text(SOURCE, encoding="utf-8", newline="\n")
    skill_dir = root / "skills" / "fixture-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fixture-skill\ndescription: Offline benchmark fixture\n---\n"
        "Check fixture_token in the isolated corpus.\n", encoding="utf-8")
    skills = SkillRegistry(None, root / "skills")
    skills.reload()
    assert skills.find_skill("fixture-skill") is not None
    embedding = EmbeddingClient(model="local-hash-256", api_key="")
    # Explicitly force offline mode even if the launching shell has embedding vars.
    embedding.base_url = ""
    embedding.provider = "local"
    rag = RagService(root, storage_dir=root / "index", embedding_client=embedding)
    indexed = rag.index_sources([corpus])
    assert indexed.chunk_count > 0 and indexed.error_count == 0, indexed

    def request_json(*args, **kwargs):
        time.sleep(web_delay_ms / 1000)
        query = kwargs["json"]["search_query"]
        return {"search_result": [
            {"title": query, "link": "https://fixture.example/page",
             "content": f"{query} {WEB_MARKER}", "media": "fixture"}]}

    def transport(request):
        assert request.url.host == "fixture.example", request.url
        time.sleep(web_delay_ms / 1000)
        html = f"<html><title>Fixture</title><body><article><h1>{WEB_MARKER}</h1>"
        html += "<p>Offline fixture documentation for code search and file tools.</p>" * 20
        return httpx.Response(200, text=html + "</article></body></html>",
                              headers={"content-type": "text/html"})

    search = ZhipuSearchProvider(api_key="fixture-not-a-real-key",
                                 search_engine="search_std", request_json=request_json)
    fetch = WebFetcher(network_policy=NetworkPolicy(resolver=lambda *_: ["8.8.8.8"]),
                       transport=httpx.MockTransport(transport))
    registry = build_default_registry(root, rag_service=rag, search_provider=search,
                                      web_fetcher=fetch, tool_batch_timeout_seconds=30)
    buffer = SkillContextBuffer()
    register_skill_tools(registry, skills, buffer)
    nested = ToolRegistry()
    nested.register(ToolDefinition("mcp__chrome-devtools__list_pages", "fixture", {},
                                   lambda: "fixture_page_1 https://fixture.example/page"))
    browser = BrowserController(BrowserSession(), FixtureBrowserManager(), nested, FixtureProbe())
    register_browser_tools(registry, browser)
    assert sorted(t.name for t in registry.list_tools()) == ALL
    return registry, buffer, browser, fetch, asdict(indexed)


def arguments(root, call):
    name, key = call["name"], call["id"]
    path = f"mutations/{key}.txt"
    if name == "read_file":
        return {"path": "corpus/module_00.py"}
    if name == "write_file":
        return {"path": path, "content": f"fixture_written_{key}\n"}
    if name == "apply_patch":
        return {"path": path, "edits": [{"old_text": "before", "new_text": "after"}]}
    if name == "delete_file":
        return {"path": path}
    if name == "list_dir":
        return {"path": "corpus", "max_entries": 100}
    if name == "glob_files":
        return {"path": "corpus", "pattern": "*.py", "max_results": 100}
    if name == "grep_code":
        return {"path": "corpus", "pattern": "fixture_token", "max_results": 200,
                "max_chars": 60000}
    if name == "execute_command":
        return {"command": [sys.executable, "-I", "-S", "-c",
                "from pathlib import Path; import hashlib; "
                "data=b''.join(p.read_bytes() for p in sorted(Path('corpus').glob('*.py'))); "
                "print('fixture_command_ok', hashlib.sha256(data).hexdigest())"],
                "timeout_seconds": 10}
    if name == "web_search":
        return {"query": "fixture code search", "top_k": 3}
    if name == "web_fetch":
        return {"url": "https://fixture.example/page", "max_chars": 8000}
    if name == "search_code":
        return {"query": "fn_0 fixture_token", "top_k": 3}
    if name == "load_skill":
        return {"name": "fixture-skill"}
    return {}


def prepare(root, batch, buffer, browser, fetch):
    buffer.clear()
    from stellarcode.web import SlidingWindowRateLimiter
    fetch.rate_limiter = SlidingWindowRateLimiter()
    browser.manager.item.config.args = []
    browser.session.switch_to_shared("fixture-shared")
    if any(c["name"] == "browser_connect" for c in batch["calls"]):
        browser.session.switch_to_isolated()
    (root / "mutations").mkdir(exist_ok=True)
    for call in batch["calls"]:
        if call["name"] in {"write_file", "apply_patch", "delete_file"}:
            (root / call["arguments"]["path"]).write_text("before\n", encoding="utf-8")


def validate(root, call, result, buffer, browser):
    if not result.success or result.timed_out:
        return False, "registry_failure"
    name, text, args = call["name"], result.result, call["arguments"]
    if name in {"write_file", "apply_patch", "delete_file"}:
        target = root / args["path"]
        if name == "delete_file":
            return not target.exists(), "deleted"
        actual = target.read_text(encoding="utf-8")
        expected = args["content"] if name == "write_file" else "after\n"
        return actual == expected, digest(actual)
    if name == "read_file":
        return text == SOURCE, digest(text)
    if name == "grep_code":
        # ripgrep traverses files concurrently: order isn't a semantic guarantee.
        # Request the FULL bounded fixture result and compare exact path/line sets.
        matches = re.findall(r"^\d+\. (corpus/module_\d+\.py):(\d+)$", text, re.M)
        actual = sorted((p, int(n)) for p, n in matches)
        expected = sorted((f"corpus/module_{i:02d}.py", 2 + 4 * j)
                          for i in range(24) for j in range(8))
        return actual == expected, digest(json.dumps(actual))
    markers = {"list_dir": "module_00.py", "glob_files": "module_00.py",
               "grep_code": "fixture_token", "execute_command": "fixture_command_ok",
               "web_search": WEB_MARKER, "web_fetch": WEB_MARKER,
               "search_code": "fn_0", "load_skill": "Loaded skill 'fixture-skill'",
               "browser_status": "chrome-devtools: ready",
               "browser_connect": "Connected to shared Chrome",
               "browser_disconnect": "Switched browser to isolated mode",
               "browser_tabs": "fixture_page_1"}
    ok = markers[name] in text
    if name == "execute_command":
        data = SOURCE.encode() * 24
        ok = ok and digest(data.decode()) in text and "exit_code: 0" in text
    if name == "load_skill":
        ok = ok and len(buffer) == 1
    if name in {"browser_connect", "browser_disconnect"}:
        ok = ok and browser.session.mode.value == (
            "shared" if name == "browser_connect" else "isolated")
    return ok, digest(text)


def p95(values):
    ordered = sorted(values)
    return ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)] if ordered else None


def summarize(rows, batches):
    paired = {}
    for row in rows:
        paired.setdefault((row["batch_id"], row["repeat"]), {})[row["mode"]] = row
    pairs = []
    for (bid, repeat), pair in paired.items():
        s, p = pair["serial"], pair["parallel"]
        matched = s["evidence"] == p["evidence"]
        pairs.append({"batch_id": bid, "repeat": repeat, "size": s["size"],
                      "serial_ms": s["latency_ms"], "parallel_ms": p["latency_ms"],
                      "equivalent": matched, "valid": s["valid"] and p["valid"] and matched})

    def metrics(selected):
        valid = [p for p in selected if p["valid"]]
        s = [p["serial_ms"] for p in valid]
        p = [p["parallel_ms"] for p in valid]
        st, pt = sum(s), sum(p)
        return {"pairs": len(selected), "valid_pairs": len(valid),
                "serial_total_ms": st, "parallel_total_ms": pt,
                "serial_mean_ms": statistics.mean(s) if s else None,
                "parallel_mean_ms": statistics.mean(p) if p else None,
                "serial_p95_ms": p95(s), "parallel_p95_ms": p95(p),
                "speedup": st / pt if pt else None,
                "latency_reduction_pct": 100 * (1 - pt / st) if st else None}

    no_web = {b["id"] for b in batches
              if not any(c["name"].startswith("web_") for c in b["calls"])}
    return {"overall": metrics(pairs),
            "by_size": {str(n): metrics([p for p in pairs if p["size"] == n])
                        for n in (2, 4, 8, 16)},
            "without_web": metrics([p for p in pairs if p["batch_id"] in no_web]),
            "successful_runs": sum(r["valid"] for r in rows), "runs": len(rows),
            "equivalent_pairs": sum(p["equivalent"] for p in pairs), "pairs": pairs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--per-size", type=int, default=25)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--web-delay-ms", type=float, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    assert args.per_size > 0 and args.repeats > 0 and args.web_delay_ms >= 0
    output = args.output or Path(__file__).parent / "results" / datetime.now().strftime(
        "%Y%m%d-%H%M%S")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = output / "workspace"
    root.mkdir()
    registry, buffer, browser, fetch, index = initialize(root, args.web_delay_ms)
    batches = generate_manifest(args.seed, args.per_size)
    for batch in batches:
        for call in batch["calls"]:
            call["arguments"] = arguments(root, call)
    metadata = {"seed": args.seed, "sizes": [2, 4, 8, 16], "per_size": args.per_size,
                "repeats": args.repeats, "parallel_workers": 4,
                "web_delay_ms": args.web_delay_ms, "python": sys.version,
                "platform": platform.platform(), "processor": platform.processor(),
                "rg": shutil.which("rg"), "index": index,
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"],
                            cwd=Path(__file__).resolve().parents[1], text=True).strip(),
                "script_sha256": digest(Path(__file__).read_text(encoding="utf-8")),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "tools": ALL,
                "coverage_calls": dict(Counter(c["name"] for b in batches for c in b["calls"])),
                "coverage_batches": dict(Counter(n for b in batches
                                                  for n in {c["name"] for c in b["calls"]})),
                "external_dependencies": "Web fixed-response transports + injected delay; "
                "browser MCP/probe doubles (zero delay); real local RAG with hash embeddings",
                "exclusions": "LLM, HITL, worktree finalization, live MCP, actual Chrome, network",
                "design": "independent calls; unique mutation targets; <=1 browser call/batch"}
    save_json(output / "manifest.json", {"metadata": metadata, "batches": batches})
    print(f"OUTPUT {output}\nTOOLS {len(ALL)}; indexed {index['chunk_count']} chunks", flush=True)
    rows = []
    total = len(batches) * args.repeats * 2
    with (output / "runs.csv").open("w", newline="", encoding="utf-8") as csvfile, (
        output / "tool_results.jsonl").open("w", encoding="utf-8") as raw:
        fields = ["batch_id", "size", "repeat", "mode", "latency_ms", "valid",
                  "failed_calls", "ordered", "evidence"]
        writer = csv.DictWriter(csvfile, fieldnames=fields)
        writer.writeheader()

        def run(batch, repeat, mode, measured=True):
            prepare(root, batch, buffer, browser, fetch)
            registry.max_parallel_tools = 1 if mode == "serial" else 4
            calls = [ToolInvocation(c["id"], c["name"], c["arguments"]) for c in batch["calls"]]
            start = time.perf_counter_ns()
            results = registry.execute_tools(calls)
            elapsed = (time.perf_counter_ns() - start) / 1e6
            if not registry.wait_for_quiescence(timeout_seconds=15):
                raise RuntimeError("Unfinished handlers; abort rather than contaminate next run")
            ordered = [r.id for r in results] == [c.id for c in calls]
            checks = [validate(root, c, r, buffer, browser)
                      for c, r in zip(batch["calls"], results)]
            valid = ordered and len(checks) == len(calls) and all(ok for ok, _ in checks)
            if not measured:
                if not valid:
                    raise RuntimeError(f"Warmup validation failed: {checks}; {results}")
                return
            row = dict(batch_id=batch["id"], size=batch["size"], repeat=repeat, mode=mode,
                       latency_ms=elapsed, valid=valid, ordered=ordered,
                       failed_calls=sum(not ok for ok, _ in checks),
                       evidence=digest(json.dumps(checks)))
            rows.append(row)
            writer.writerow(row)
            csvfile.flush()
            raw.write(json.dumps({"run": row, "checks": checks,
                                  "tools": [asdict(r) for r in results]}) + "\n")
            raw.flush()
            if not valid:
                print(f"VALIDATION_FAILURE {batch['id']} {mode} {checks}", flush=True)

        # Exercise every tool before timing: imports, SQLite, HTML parser and subprocess startup.
        for name in ALL:
            source = next(b for b in batches if any(c["name"] == name for c in b["calls"]))
            one = next(c for c in source["calls"] if c["name"] == name)
            warm = {"id": "warmup", "size": 1, "calls": [one]}
            run(warm, -1, "serial", measured=False)
        for mode in ("serial", "parallel"):
            run(batches[0], -1, mode, measured=False)
        for bi, batch in enumerate(batches):
            for repeat in range(args.repeats):
                modes = ["serial", "parallel"] if (bi + repeat) % 2 == 0 else ["parallel", "serial"]
                for mode in modes:
                    run(batch, repeat, mode)
            if (bi + 1) % 5 == 0:
                print(f"PROGRESS {len(rows)}/{total}", flush=True)
    summary = summarize(rows, batches)
    save_json(output / "summary.json", summary)
    overall = summary["overall"]
    lines = ["# Tool Parallelism 实验结果", "", "## 范围与方法", "",
             f"- 代码：`{metadata['commit']}`；随机种子：{args.seed}。",
             f"- {len(batches)} 个固定混合批次，大小 2/4/8/16，每批串并行各 {args.repeats} 次。",
             "- 真实 ToolRegistry.execute_tools；最大并发数 1 对比 4；不经过 LLM/HITL。",
             "- 11 个默认工具 + load_skill + 4 个浏览器管理工具，完整名单及覆盖次数见 manifest。",
             "- 文件/命令/代码搜索/RAG/Skill 执行真实实现；RAG 为本地哈希向量、预建 SQLite 索引。",
             f"- Web 使用固定响应传输，每次模拟等待 {args.web_delay_ms:g}ms；保留解析/格式化实现。",
             "- 浏览器保留真实 controller，但 MCP 管理器/连接探针/页面响应是零延迟替身。",
             "- 每批至多一个浏览器操作；写入目标独立；每次恢复输入和可变状态。",
             "- 全工具预热不计时；交替串并行顺序；只计提交到全部结果返回的墙钟时间。",
             "- grep 验证完整 192 个命中的路径/行号集合，不要求 ripgrep 文件遍历顺序相同。",
             "- 此结果不代表真实公网/浏览器连接速度、完整 Agent 加速或冲突操作安全性。", "",
             "## 结果", "",
             f"有效执行：{summary['successful_runs']}/{summary['runs']}；"
             f"一致配对：{summary['equivalent_pairs']}/{len(summary['pairs'])}。", "",
             "| 批次大小 | 配对数（有效） | 串行均值 ms | 并行均值 ms | 串行 P95 ms | 并行 P95 ms | 加速比 | 耗时降低 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for label, m in list(summary["by_size"].items()) + [("总体", overall), ("不含 Web 批次", summary["without_web"])]:
        if m["valid_pairs"]:
            lines.append(f"| {label} | {m['pairs']} ({m['valid_pairs']}) | {m['serial_mean_ms']:.2f} | "
                         f"{m['parallel_mean_ms']:.2f} | {m['serial_p95_ms']:.2f} | "
                         f"{m['parallel_p95_ms']:.2f} | {m['speedup']:.3f}x | {m['latency_reduction_pct']:.2f}% |")
    lines += ["", "总体加速比 = 有效配对串行耗时总和 / 并行耗时总和；不是各批加速比的算术平均。",
              "P95 为有效运行耗时的 nearest-rank 分位数。失败运行保留在原始记录中，不算作加速收益。",
              "", "## 文件", "", "- manifest.json：完整调用、参数、覆盖、环境、脚本哈希。",
              "- runs.csv：每次批次运行的计时与验证结果。",
              "- tool_results.jsonl：每个工具原始结果、success、超时和校验证据。",
              "- summary.json：总体、分组和每对结果。",
              "- workspace/：生成的测试文件和索引；delete_file 只删除这里的可再生测试文件。",
              "", "三次重复仅用于初步工程测量，随机工具分布不等于实际用户调用分布。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"overall": overall, "valid": summary["successful_runs"],
                      "runs": summary["runs"]}, ensure_ascii=False), flush=True)
    if summary["successful_runs"] != total or overall["valid_pairs"] != total // 2:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
