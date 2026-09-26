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


def clean_process_environment():
    # PATHEXT is essential for PowerShell to recognize native executable commands.
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "PATH", "PATHEXT", "COMSPEC"}
    return {k.upper(): v for k, v in os.environ.items() if k.upper() in allowed}


def execute_tests(workspace, private=False):
    command = ([sys.executable, "-I", "-c", ACCEPTANCE, str(workspace)] if private else
               [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-v"])
    # Do not pass model keys to generated test code. This is not an OS sandbox.
    environment = clean_process_environment()
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
        self.call_limit = 60

    def chat(self, messages, tools=None, temperature=0.2, on_delta=None):
        with self.lock:
            if self.count >= self.call_limit or sum(r.get("input_tokens", 0) for r in self.records) > 2_000_000:
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
    parser.add_argument("--todo-snapshot", type=Path, help="Historical task snapshot metadata")
    parser.add_argument("--source-trace", type=Path, help="Trace containing original Todo task.submit")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to make real model API calls")
    if bool(args.todo_snapshot) != bool(args.source_trace):
        parser.error("--todo-snapshot and --source-trace must be supplied together")
    root = Path(__file__).resolve().parents[1]
    prefix = "todo-replay-" if args.todo_snapshot else "team-latency-"
    output = root / "experiments" / "results" / (prefix + datetime.now().strftime("%Y%m%d-%H%M%S"))
    workspace = output / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    prompt = SPEC
    fixture = {}
    validator = execute_tests
    if args.todo_snapshot:
        from todo_latency_fixture import restore, validate
        prompt, fixture = restore(args.todo_snapshot, args.source_trace, workspace)
        validator = validate
    else:
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
    if args.todo_snapshot:
        from stellarcode.tools import build_default_registry, ToolRegistry
        from stellarcode.tools.builtin import _execute_command, _reject_detached_task_command
        from stellarcode.benchmark import _relative_workspace_path
        raw = build_default_registry(workspace)
        registry = ToolRegistry()
        allowed = {"read_file", "write_file", "apply_patch", "list_dir", "glob_files", "grep_code", "delete_file"}
        def wrap(definition):
            def handler(**kwargs):
                _relative_workspace_path(workspace, kwargs.get("path", "."))
                return definition.handler(**kwargs)
            return ToolDefinition(definition.name, definition.description, definition.parameters, handler)
        for definition in raw.list_tools():
            if definition.name in allowed:
                registry.register(wrap(definition))
        definition = next(t for t in raw.list_tools() if t.name == "execute_command")
        environment = clean_process_environment()
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
        def command(command, timeout_seconds=30):
            _reject_detached_task_command(command)
            return _execute_command(workspace, command, min(60, max(1, timeout_seconds)), environment=environment)
        registry.register(ToolDefinition(definition.name, definition.description, definition.parameters, command))
        client.call_limit = 100
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
    timer = threading.Timer(1800 if args.todo_snapshot else 900, cancel.set)
    timer.daemon = True
    os.environ["TEAM_PYTHON_EXECUTABLE"] = sys.executable
    agent = AgentOrchestrator(client, registry, worker_count=2, max_iterations_per_agent=16 if args.todo_snapshot else 10,
                              workspace=workspace, message_bus_dir=output / "bus", event_callback=event)
    save(output / "manifest.json", {"model": client.model, "budget": "low / 4096,8192,4096 defaults",
         "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
         "workload": "Historical Todo API replay" if args.todo_snapshot else "stdlib inventory and CSV",
         "limitations": "No desktop/HITL/Git merge or OS sandbox. Todo: sanitized snapshot, no skills/MCP/web, current deps; 100 calls / 30min. Inventory: 60 calls / 15min.",
         "fixture": fixture, "spec": prompt})
    print(f"OUTPUT {output}", flush=True)
    timer.start()
    error = None
    answer = ""
    try:
        operational = ("\n\nBenchmark workspace: " + str(workspace) +
                       ". Operate only here; do not install packages or start background processes. "
                       "Python/FastAPI/httpx/uvicorn are available. No network work is needed.") if args.todo_snapshot else ""
        answer = agent.run(prompt + operational, cancel)
    except Exception as exc:
        error = str(exc).replace(client.api_key, "[redacted]") if client.api_key else str(exc)
    finally:
        timer.cancel()
    elapsed = time.perf_counter() - start
    (output / "answer.md").write_text(answer or error or "", encoding="utf-8")
    try:
        public = validator(workspace)
        private = validator(workspace, private=True)
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
              "passed": not error and public["exit_code"] == 0 and public["test_count"] >= (8 if args.todo_snapshot else 12)
                        and private["exit_code"] == 0 and private["test_count"] == (12 if args.todo_snapshot else 9),
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
