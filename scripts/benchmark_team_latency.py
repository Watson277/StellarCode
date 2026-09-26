"""Opt-in, one paid Team run in an isolated fixture directory, not a speedup claim."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from dotenv import dotenv_values

from stellarcode.benchmark import build_benchmark_registry
from stellarcode.llm.compatible_client import OpenAICompatibleClient
from stellarcode.llm.team_budget import team_request_options
from stellarcode.llm.types import current_llm_operation
from stellarcode.multi_agent import AgentOrchestrator
from stellarcode.tools import ToolDefinition


SPEC = """Implement a small standard-library-only inventory module. Files you may edit:
inventory.py, report.py, test_inventory.py, test_report.py, README.md.
Do not install anything, use the network, or explore outside this workspace.
Read SPEC.md first, then implement and actually test the following fixed interfaces.
inventory.Inventory(initial: dict[str,int]): copy input; reject empty/blank SKU or
non-integer/negative stock (bool is not an integer here), using ValueError.
reserve(sku, quantity) and release(sku, quantity): quantity is a positive integer,
reject bool; unknown SKU raises KeyError. reserve raises ValueError for insufficient
stock without changing state. release adds stock. Both return the new stock count.
snapshot(): return a fresh dict; caller mutation must not affect the inventory.
report.render_inventory(stock: dict[str,int]) -> str: pure function, alphabetically
sorted SKU, CSV header sku,quantity, standard CSV escaping, LF line endings including
final newline; empty dict returns header only. Do not mutate input.
The two implementation modules are independent after this contract; use two workers
where appropriate and avoid overlapping writes. Each module owner also writes its
corresponding unittest file (test_inventory.py or test_report.py). Cover happy paths,
validation, no-mutation and escaping, at least 12 test methods total. Finish with one
integration/documentation step: run_tests and a short README with usage/test commands.
The run_tests tool takes no arguments, executes the fixed unittest command with the
same interpreter, and returns exit code, collected count and output. No shell tool is
available or needed. Reviewer is managed by the orchestrator, not a plan step.
Keep scope tight: no HTTP server, database, concurrency feature, packaging or extra deps.
"""

# Outside the agent-readable workspace: these assertions are never in its prompt/tools.
ACCEPTANCE = '''import csv, io, sys, unittest
sys.path.insert(0, sys.argv[1])
from inventory import Inventory
from report import render_inventory
class Acceptance(unittest.TestCase):
    def test_stock(self):
        obj=Inventory({"A":5}); self.assertEqual(obj.reserve("A",2),3)
        self.assertEqual(obj.release("A",4),7)
    def test_copy(self):
        source={"A":5}; obj=Inventory(source); source["A"]=8
        copy=obj.snapshot(); copy["A"]=9; self.assertEqual(obj.snapshot(),{"A":5})
    def test_initial(self):
        for value in ({"":1},{" ":1},{"A":-1},{"A":True},{"A":1.5}):
            with self.subTest(value=value), self.assertRaises(ValueError): Inventory(value)
    def test_quantity(self):
        for method in ("reserve","release"):
            for qty in (0,-1,True,1.5):
                with self.subTest(method=method,qty=qty), self.assertRaises(ValueError):
                    getattr(Inventory({"A":5}),method)("A",qty)
    def test_unknown(self):
        for method in ("reserve","release"):
            with self.assertRaises(KeyError): getattr(Inventory({"A":5}),method)("B",1)
    def test_insufficient(self):
        obj=Inventory({"A":2})
        with self.assertRaises(ValueError): obj.reserve("A",3)
        self.assertEqual(obj.snapshot(),{"A":2})
    def test_report(self):
        stock={"B":2,"A":1}; self.assertEqual(render_inventory(stock),"sku,quantity\\nA,1\\nB,2\\n")
        self.assertEqual(stock,{"B":2,"A":1})
    def test_empty(self): self.assertEqual(render_inventory({}),"sku,quantity\\n")
    def test_escape(self):
        text=render_inventory({'A,"quoted"':3})
        self.assertEqual(list(csv.reader(io.StringIO(text))),[["sku","quantity"],['A,"quoted"',"3"]])
        self.assertTrue(text.endswith("\\n")); self.assertNotIn("\\r",text)
unittest.main(argv=["acceptance"],verbosity=2)
'''


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def execute_tests(workspace, private=False):
    command = ([sys.executable, "-I", "-c", ACCEPTANCE, str(workspace)] if private else
               [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-v"])
    # Do not pass model keys to generated test code. This is not an OS sandbox.
    environment = {k: v for k, v in os.environ.items()
                   if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "COMSPEC"}}
    result = subprocess.run(command, cwd=workspace, env=environment, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=30)
    output = result.stdout + result.stderr
    count = re.search(r"Ran (\d+) tests?", output)
    return {"exit_code": result.returncode, "test_count": int(count[1]) if count else 0,
            "output": output[-12000:]}


class RecordedClient(OpenAICompatibleClient):
    def __init__(self, config, output):
        super().__init__(api_key=config.get("LLM_API_KEY") or "", model=config["LLM_MODEL_NAME"],
                         base_url=config["LLM_BASE_URL"], timeout_seconds=90, max_retries=0)
        self.output = output
        self.records = []
        self.lock = threading.Lock()
        self.count = 0

    def chat(self, messages, tools=None, temperature=0.2, on_delta=None):
        with self.lock:
            if self.count >= 60 or sum(r.get("input_tokens", 0) for r in self.records) > 1_000_000:
                raise RuntimeError("Benchmark call/input safety budget reached")
            self.count += 1
            number = self.count
        start = time.perf_counter()
        record = {"id": number, "role": current_llm_operation(), "options": team_request_options()}
        print(f"CALL {number} {record['role']}", flush=True)
        try:
            result = super().chat(messages, tools, temperature, on_delta)
            record.update(asdict(result.usage), ok=True)
            return result
        except Exception as exc:
            error = str(exc)
            if self.api_key:
                error = error.replace(self.api_key, "[redacted]")
            record.update(ok=False, error=error)
            raise
        finally:
            record["seconds"] = time.perf_counter() - start
            with self.lock:
                self.records.append(record)
                with (self.output / "calls.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
            print(f"RETURN {number} {record['seconds']:.1f}s ok={record['ok']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize one paid run")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to make real model API calls")
    root = Path(__file__).resolve().parents[1]
    output = root / "experiments" / "results" / ("team-latency-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    workspace = output / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / "SPEC.md").write_text(SPEC, encoding="utf-8")
    config = dotenv_values(root / ".env")
    client = RecordedClient(config, output)
    registry = build_benchmark_registry(workspace, (
        "inventory.py", "report.py", "test_inventory.py", "test_report.py", "README.md",
    ))
    registry.register(ToolDefinition(
        "run_tests", "Run fixed unittest discovery in this workspace; no shell arguments.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda: json.dumps(execute_tests(workspace)),
    ))
    events = []
    event_lock = threading.Lock()
    start = time.perf_counter()

    def event(kind, data):
        if kind == "team.agent.delta":
            return
        entry = {"seconds": time.perf_counter() - start, "event": kind, "data": data}
        with event_lock:
            events.append(entry)
            with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if kind == "team.agent.status":
            print(f"STAGE {data['agent_name']} {data['team_task_id']} {data['status']}", flush=True)

    cancel = threading.Event()
    timer = threading.Timer(900, cancel.set)
    timer.daemon = True
    os.environ["TEAM_PYTHON_EXECUTABLE"] = sys.executable
    agent = AgentOrchestrator(client, registry, worker_count=2, max_iterations_per_agent=10,
                              workspace=workspace, message_bus_dir=output / "bus", event_callback=event)
    save(output / "manifest.json", {"model": client.model, "budget": "low / 4096,8192,4096 defaults",
         "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
         "workload": "stdlib inventory and CSV, not the previous Todo API workload",
         "limitations": "No desktop/HITL/Git merge, no OS sandbox, no baseline; max 60 calls / 15min",
         "spec": SPEC})
    print(f"OUTPUT {output}", flush=True)
    timer.start()
    error = None
    answer = ""
    try:
        answer = agent.run(SPEC, cancel)
    except Exception as exc:
        error = str(exc).replace(client.api_key, "[redacted]") if client.api_key else str(exc)
    finally:
        timer.cancel()
    elapsed = time.perf_counter() - start
    (output / "answer.md").write_text(answer or error or "", encoding="utf-8")
    try:
        public = execute_tests(workspace)
        private = execute_tests(workspace, private=True)
    except Exception as exc:
        public = private = {"exit_code": -1, "test_count": 0, "output": str(exc)}
    save(output / "validation.json", {"generated_tests": public, "private_acceptance": private})
    stages, active = [], {}
    for entry in events:
        if entry["event"] != "team.agent.status":
            continue
        data = entry["data"]
        key = (data["agent_name"], data["team_task_id"])
        if data["status"] == "working":
            active[key] = entry["seconds"]
        elif key in active and data["status"] in {"completed", "failed"}:
            stages.append({"agent": key[0], "task": key[1],
                           "seconds": entry["seconds"] - active.pop(key), "status": data["status"]})
    result = {"elapsed_seconds": elapsed, "error": error,
              "passed": not error and public["exit_code"] == 0 and public["test_count"] >= 12
                        and private["exit_code"] == 0 and private["test_count"] == 9,
              "model_calls_started": client.count, "model_calls_recorded": len(client.records),
              "stages": stages, "repair_rounds": agent.review_retries,
              "usage": {key: sum(r.get(key, 0) for r in client.records) for key in
                        ("input_tokens", "output_tokens", "reasoning_tokens", "cached_input_tokens")},
              "usage_complete": len(client.records) == client.count and all(r.get("exact") for r in client.records),
              "tests": public["test_count"], "private_checks": private["test_count"]}
    save(output / "result.json", result)
    print("RESULT " + json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
