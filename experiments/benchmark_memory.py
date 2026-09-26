"""Real-Agent, real-GLM paired context-compaction experiment. Pilot by default."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

from dotenv import dotenv_values

from stellarcode.agent import Agent
from stellarcode.benchmark import BENCHMARK_SYSTEM_PROMPT, build_benchmark_registry
from stellarcode.llm.compatible_client import OpenAICompatibleClient
from stellarcode.llm.types import current_llm_operation, estimate_request_tokens
from stellarcode.memory.history_compactor import ConversationHistoryCompactor

from memory_tasks import TASKS, build_workspace


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class NoCompaction(ConversationHistoryCompactor):
    def needs_compaction(self, messages, tools=None):
        return False

    def maybe_compact(self, messages, tools, client, cancellation_event=None):
        return None


class RecordedClient(OpenAICompatibleClient):
    """Production transport/parser with experiment-only output limit and metering."""
    def __init__(self, config, directory):
        super().__init__(api_key=config["LLM_API_KEY"], model=config["LLM_MODEL_NAME"],
                         base_url=config["LLM_BASE_URL"], timeout_seconds=180, max_retries=0)
        self.directory = directory
        self.records = []
        self.turn = 0
        self.failures = 0

    def _stream_chat(self, httpx, payload, headers, on_delta):
        payload["max_tokens"] = 8192
        payload["reasoning_effort"] = "low"
        return super()._stream_chat(httpx, payload, headers, on_delta)

    def chat(self, messages, tools=None, temperature=0, on_delta=None):
        estimate = estimate_request_tokens(messages, tools)
        # Conservative safety stop well below declared 1M. Never truncate control history.
        if estimate > 700_000:
            self.failures += 1
            raise RuntimeError("Experiment safety limit: request estimate exceeds 700K")
        if len(self.records) >= 120 or sum(r.get("input_tokens", 0) for r in self.records) > 8_000_000:
            self.failures += 1
            raise RuntimeError("Experiment per-run call/input budget exceeded")
        number = len(self.records) + 1
        operation = current_llm_operation()
        start = time.perf_counter()
        payload = {"messages": messages, "tools": tools, "model": self.model,
                   "temperature": temperature, "max_tokens": 8192, "reasoning_effort": "low"}
        # Only synthetic task data are recorded. Headers and .env values never are.
        save(self.directory / f"request-{number:03d}.json", payload)
        record = {"call": number, "turn": self.turn, "operation": operation,
                  "estimated_input_tokens": estimate, "model": self.model}
        print(f"CALL turn={self.turn} n={number} op={operation} estimated={estimate}", flush=True)
        try:
            result = super().chat(messages, tools, temperature, on_delta or (lambda _: None))
            record.update(asdict(result.usage))
            record["output_budget_reached"] = result.usage.output_tokens >= 8192
            record["ok"] = True
            save(self.directory / f"response-{number:03d}.json", asdict(result))
            return result
        except Exception as exc:
            self.failures += 1
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}".replace(self.api_key, "[redacted]"))
            raise
        finally:
            record["elapsed_seconds"] = time.perf_counter() - start
            self.records.append(record)
            with (self.directory / "calls.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"RETURN n={number} ok={record['ok']} input={record.get('input_tokens')} "
                  f"output={record.get('output_tokens')} seconds={record['elapsed_seconds']:.1f}", flush=True)


def evaluate(workspace, task, directory):
    # Private evaluator is not placed inside the Agent-readable workspace.
    command = [sys.executable, "-I", str(Path(__file__).with_name("memory_tasks.py")),
               str(workspace), task]
    try:
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True,
                                encoding="utf-8", timeout=20)
        (directory / "acceptance-output.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            return [{"name": "import_or_test_execution", "kind": "functional", "passed": False,
                     "error": result.stderr[-3000:]}]
        return json.loads(result.stdout)
    except Exception as exc:
        return [{"name": "test_execution", "kind": "functional", "passed": False,
                 "error": f"{type(exc).__name__}: {exc}"}]


def run_one(output, config, task, repeat, mode):
    directory = output / f"{task}-{repeat}-{mode}"
    directory.mkdir()
    workspace = directory / "workspace"
    prompts = build_workspace(workspace, task)
    save(directory / "prompts.json", prompts)
    client = RecordedClient(config, directory)
    registry = build_benchmark_registry(workspace, ("solution.py",))
    events = []
    def event(name, data):
        events.append({"turn": client.turn, "event": name, "data": data})
        if name == "history.compacted":
            print(f"COMPACTION {json.dumps(data, ensure_ascii=False)}", flush=True)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(events[-1], ensure_ascii=False) + "\n")

    agent = Agent(client, registry, system_prompt=BENCHMARK_SYSTEM_PROMPT,
                  workspace=workspace, max_iterations=8, memory_manager=None,
                  context_window=1_000_000, stream_output=True, temperature=0,
                  rag_auto_retrieval=False, event_callback=event,
                  progress_callback=lambda _: None)
    if mode == "control":
        agent.history_compactor = NoCompaction(context_window=1_000_000)
    else:
        agent.history_compactor.compression_threshold_ratio = 51_200 / 1_000_000
    rounds = []
    start = time.perf_counter()
    error = None
    print(f"RUN {task} repeat={repeat} mode={mode}", flush=True)
    try:
        for turn, prompt in enumerate(prompts, 1):
            client.turn = turn
            before = len(client.records)
            reply = agent.run(prompt)
            rounds.append({"turn": turn, "reply": reply,
                           "calls": len(client.records) - before,
                           "context_estimate": estimate_request_tokens(agent.messages, registry.schemas())})
            save(directory / "rounds.json", rounds)
            save(directory / "messages.json", agent.messages)
            (directory / f"solution-round-{turn:02d}.py").write_text(
                (workspace / "solution.py").read_text(encoding="utf-8"), encoding="utf-8")
            print(f"ROUND_DONE {task}/{mode} {turn}/12", flush=True)
            if client.failures:
                raise RuntimeError("Provider call failed; see calls.jsonl")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}".replace(config["LLM_API_KEY"], "[redacted]")
    elapsed = time.perf_counter() - start
    checks = evaluate(workspace, task, directory)
    save(directory / "acceptance.json", checks)
    main = [r for r in client.records if r["operation"] != "history-compaction" and r["ok"]]
    summaries = [r for r in client.records if r["operation"] == "history-compaction" and r["ok"]]
    compactions = [e for e in events if e["event"] == "history.compacted"]
    constraints = [c for c in checks if c["kind"] == "constraint"]
    result = {"task": task, "repeat": repeat, "mode": mode, "error": error,
              "rounds_completed": len(rounds), "passed": not error and len(rounds) == 12 and all(c["passed"] for c in checks),
              "checks_passed": sum(c["passed"] for c in checks), "checks_total": len(checks),
              "constraints_passed": sum(c["passed"] for c in constraints), "constraints_total": len(constraints),
              "main_calls": len(main), "summary_calls": len(summaries),
              "main_input_tokens": sum(r["input_tokens"] for r in main),
              "summary_input_tokens": sum(r["input_tokens"] for r in summaries),
              "total_input_tokens": sum(r.get("input_tokens", 0) for r in client.records),
              "total_output_tokens": sum(r.get("output_tokens", 0) for r in client.records),
              "cached_input_tokens": sum(r.get("cached_input_tokens", 0) for r in client.records),
              "exact_usage": all(r.get("exact", False) for r in client.records),
              "max_main_prompt_tokens": max((r["input_tokens"] for r in main), default=0),
              "mean_main_prompt_tokens": sum(r["input_tokens"] for r in main) / max(1, len(main)),
              "compactions": len(compactions), "compaction_methods": [e["data"]["method"] for e in compactions],
              "empty_reply_rounds": [r["turn"] for r in rounds if not r["reply"].strip()],
              "output_budget_hits": sum(r.get("output_budget_reached", False) for r in client.records),
              "elapsed_seconds": elapsed,
              "summary_seconds": sum(r["elapsed_seconds"] for r in summaries)}
    save(directory / "result.json", result)
    print("RUN_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = dotenv_values(root / ".env")
    for name in ("LLM_MODEL_NAME", "LLM_BASE_URL", "LLM_API_KEY"):
        if not config.get(name):
            raise RuntimeError(f"Missing configuration: {name}")
    if "glm" not in config["LLM_MODEL_NAME"].lower():
        raise RuntimeError("Expected explicitly configured GLM model")
    output = (args.output or Path(__file__).parent / "results" / (
        "memory-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=False)
    save(output / "manifest.json", {
        "model": config["LLM_MODEL_NAME"], "base_url": config["LLM_BASE_URL"],
        "declared_context_window": 1_000_000, "compression_trigger_estimated_tokens": 51_200,
        "retain_recent_turns": 3, "max_output_tokens": 8192, "temperature": 0,
        "reasoning_effort": "low", "thinking_disabled": False,
        "long_term_memory": "disabled", "tools": "6 workspace-guarded coding tools",
        "max_iterations_per_round": 8, "rounds_per_run": 12,
        "formal": args.formal, "repetitions": 3 if args.formal else 1,
        "python": sys.version, "platform": platform.platform(),
        "time": datetime.now(timezone.utc).isoformat(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "scripts": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [Path(__file__), Path(__file__).with_name("memory_tasks.py")]},
        "workload": "Synthetic integration records read through real tools; fixed valid "
        "data per round. Controlled load, not a claim of naturally occurring task distribution.",
        "api_retries": 0, "streaming": True})
    print(f"OUTPUT {output}", flush=True)
    results = []
    for ti, task in enumerate(TASKS if args.formal else ["config"]):
        for repeat in range(1, 4 if args.formal else 2):
            order = ["control", "compact"] if (ti + repeat) % 2 else ["compact", "control"]
            for mode in order:
                result = run_one(output, config, task, repeat, mode)
                results.append(result)
                save(output / "results.json", results)
                if result["error"]:
                    print("STOP: infrastructure/model error; do not spend on further runs", flush=True)
                    return 1
    if not args.formal:
        compact = next(r for r in results if r["mode"] == "compact")
        control = next(r for r in results if r["mode"] == "control")
        gate = {"both_completed": all(r["rounds_completed"] == 12 and not r["error"] for r in results),
                "llm_compaction_observed": "llm" in compact["compaction_methods"],
                "control_below_window": control["max_main_prompt_tokens"] < 700_000,
                "exact_usage": all(r["exact_usage"] for r in results),
                "both_acceptance_passed": all(r["passed"] for r in results)}
        save(output / "pilot-gate.json", gate)
        print("PILOT_GATE " + json.dumps(gate), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
