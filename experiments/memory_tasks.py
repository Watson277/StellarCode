"""Fixed multi-turn tasks and private acceptance checks for compaction experiments."""
import ast
import hashlib
import json
from pathlib import Path


CONFIG_STEPS = [
    "Implement load_config(path, overrides=None) in solution.py: read a JSON object. "
    "Persistent requirements: only Python standard library; preserve unknown fields; never mutate "
    "caller inputs; validation errors must be ValueError containing the offending field name; "
    "relative output_dir paths resolve against the configuration file's directory, not cwd.",
    "Add defaults retries=3, timeout=30, enabled=True for absent fields in load_config.",
    "Validate retries as a non-bool integer >=0, timeout as a non-bool positive number, enabled "
    "as bool. Reject a non-object root with ValueError mentioning root.",
    "Support overrides as a dictionary applied after reading the file and before validation.",
    "Accept legacy retry_count: convert to retries only when retries is absent, remove retry_count.",
    "Implement output_dir normalization when present, returning an absolute string path.",
    "Normalize optional tags: require a list of strings, return sorted unique tags.",
    "Upgrade overrides to recursive dictionary merge; non-dictionary values replace the old value.",
    "Add redact_config(config): return a recursive copy masking values of keys token/password/api_key "
    "with '***', including dictionaries inside lists. Other keys remain unchanged.",
    "Add config_fingerprint(config): SHA256 hex of json.dumps(config,sort_keys=True," 
    "separators=(',', ':'),ensure_ascii=False).encode('utf-8').",
    "Add load_config_layers(paths): load each using load_config, recursively merge left to right; "
    "paths is nonempty. Later loaded values (including defaults) override earlier values.",
    "Add describe_config(config): return {'keys': sorted top-level key names, 'fingerprint': "
    "config_fingerprint(config)}. Finish the implementation and check consistency with earlier work.",
]

CSV_STEPS = [
    "Implement clean_rows(rows) in solution.py for a list of dictionaries. Persistent requirements: "
    "standard library only; preserve unknown fields; never mutate caller rows; empty/missing id "
    "raises ValueError mentioning id; output is sorted lexicographically by normalized string id. "
    "Normalize id and name by str(...).strip(); missing name becomes ''.",
    "Normalize status: missing/empty means pending; otherwise strip and lowercase the string.",
    "Duplicate normalized ids: retain the last row, then sort the output by id.",
    "Add summarize(rows): clean first, return {'total': count, 'by_status': dictionary of counts}.",
    "Add read_csv(path): read UTF-8 CSV using DictReader and return clean_rows of its rows.",
    "Add write_csv(path, rows): clean rows then write UTF-8 CSV, header id,name,status followed by "
    "all other keys sorted alphabetically. Missing cells are empty strings.",
    "Add active_only(rows): clean rows then select status == active.",
    "Add merge_rows(left,right): clean their concatenation; right-hand duplicate ids win.",
    "Add group_by_status(rows): clean then return a dictionary mapping status to lists of rows.",
    "Add rows_checksum(rows): SHA256 hex of json.dumps(clean_rows(rows),sort_keys=True," 
    "separators=(',', ':'),ensure_ascii=False).encode('utf-8').",
    "Add select_ids(rows, ids): clean rows, retain ids in the supplied collection after converting "
    "each selector to stripped string. Keep normal output order.",
    "Add to_json(rows): json.dumps(clean_rows(rows),ensure_ascii=False,sort_keys=True). "
    "Finish implementation and check consistency with earlier work.",
]

TASK_STEPS = [
    "Implement TaskStore(path) in solution.py backed by a JSON list. Persistent requirements: "
    "standard library only; preserve unknown keys in loaded tasks; task ids are stripped strings; "
    "all validation failures are ValueError mentioning the relevant field; public methods must "
    "not expose mutable internal dictionaries. Load existing file; absent file means empty. "
    "Implement list_tasks() returning a copy sorted lexicographically by id.",
    "Add add(id,title): strip id/title; reject empty id/title and duplicate id. New status pending. "
    "Return a copy of the new task.",
    "Add save(): persist tasks as UTF-8 JSON with ensure_ascii=False, create parent dirs if needed.",
    "Add get(id): return a copy or raise ValueError mentioning id.",
    "Add update(id, **fields): merge fields, except id changes are rejected; strip and validate "
    "title when supplied. Preserve all other keys. Return a copy of updated task.",
    "Constrain status on add/update/load to pending, active, done; missing loaded status is pending. "
    "Reject other values with ValueError mentioning status.",
    "Add remove(id): remove task and return a copy; unknown id is ValueError mentioning id.",
    "Add list_by_status(status): filtered copies in the same order as list_tasks.",
    "Add counts(): return counts for all three statuses (including zeros).",
    "Add complete_many(ids): validate all ids exist before changing anything, then mark them done.",
    "Add export_json(): return json.dumps(list_tasks(),ensure_ascii=False,sort_keys=True).",
    "Add rename(id,title): same validation as update; return updated task copy. "
    "Finish implementation and check consistency with earlier work.",
]
TASKS = {"config": CONFIG_STEPS, "csv": CSV_STEPS, "tasks": TASK_STEPS}


def fixture_text(task, turn, count=150):
    """Distinct, reproducible integration inputs, not repeated padding paragraphs.

    These are synthetic load-bearing documentation attachments. The benchmark
    explicitly measures this controlled long-context workload, not organic usage.
    """
    records = []
    for i in range(count):
        key = turn * count + i
        if task == "config":
            row = {"case": key, "source": f"deployments/region-{i % 17}/service-{key}.json",
                   "config": {"retries": i % 7, "timeout": i % 90 + 1,
                              "enabled": i % 2 == 0, "component": f"service_{key}",
                              "metadata": {"owner": f"team-{i % 23}", "revision": key}},
                   "integration_note": f"Deployment {key} is consumed by worker {i % 31}."}
        elif task == "csv":
            row = {"case": key, "source": f"imports/batch-{turn}/row-{i}",
                   "row": {"id": f" {key} ", "name": f" Product {key} ",
                           "status": ["pending", "active", "done"][i % 3],
                           "warehouse": f"zone-{i % 17}", "quantity": str(i % 80)},
                   "integration_note": f"Import partition {i % 19} contains row {key}."}
        else:
            row = {"case": key, "source": f"queues/team-{i % 13}/task-{key}.json",
                   "task": {"id": str(key), "title": f"Review delivery {key}",
                            "status": ["pending", "active", "done"][i % 3],
                            "owner": f"worker-{i % 17}", "revision": key},
                   "integration_note": f"Queue shard {i % 29} schedules delivery {key}."}
        records.append(json.dumps(row, ensure_ascii=False))
    return (f"# {task} integration corpus, round {turn}\n"
            "Synthetic representative inputs for this module. They are data, not instructions.\n"
            "Use representative records to inspect compatibility; do not hard-code these values.\n"
            + "\n".join(records) + "\n")


def build_workspace(workspace, task):
    workspace.mkdir(parents=True)
    (workspace / "solution.py").write_text('"""Implement the requested module here."""\n')
    docs = workspace / "docs"
    docs.mkdir()
    prompts = []
    for turn, step in enumerate(TASKS[task], 1):
        text = fixture_text(task, turn)
        (docs / f"round_{turn:02d}.md").write_text(text, encoding="utf-8")
        prompts.append(
            f"Round {turn}/12. {step}\n"
            f"Read docs/round_{turn:02d}.md in full using read_file max_chars=200000 "
            "as the integration corpus for this change. Inspect solution.py as needed. "
            "Implement only this round's requested change in solution.py, retain earlier behavior, "
            "and give a short completion summary. Do not reproduce the large corpus in your answer.")
    return prompts


def acceptance(module, work):
    """Private black-box functional and persistent-constraint checks, outside Agent tools."""
    checks = []

    def check(name, kind, callback):
        try:
            assert callback() is not False
            checks.append({"name": name, "kind": kind, "passed": True})
        except Exception as exc:
            checks.append({"name": name, "kind": kind, "passed": False,
                           "error": f"{type(exc).__name__}: {exc}"})

    def equal(actual, expected):
        assert actual == expected, f"{actual!r} != {expected!r}"

    def rejects(callback, field):
        try:
            callback()
        except ValueError as exc:
            assert field in str(exc)
            return
        raise AssertionError("Expected ValueError")

    tree = ast.parse((work / "solution.py").read_text(encoding="utf-8"))
    import sys
    imports = [n.names[0].name.split('.')[0] if isinstance(n, ast.Import)
               else (n.module or '').split('.')[0]
               for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    check("stdlib_only", "constraint", lambda: set(imports) <= sys.stdlib_module_names)
    fixture = work / "private-input.json"
    task = module.__bench_task__
    if task == "config":
        fixture.write_text(json.dumps({"custom": {"x": 7}, "output_dir": "artifacts"}))
        check("defaults", "functional", lambda: equal(module.load_config(fixture)["retries"], 3))
        check("unknown_fields", "constraint", lambda: equal(module.load_config(fixture)["custom"], {"x": 7}))
        check("relative_path", "constraint", lambda: equal(module.load_config(fixture)["output_dir"], str((work / "artifacts").resolve())))
        check("error_field", "constraint", lambda: rejects(lambda: module.load_config(fixture, {"retries": True}), "retries"))
        def no_mutation():
            d = {"custom": {"y": 8}}
            result = module.load_config(fixture, d)
            equal(result["custom"], {"x": 7, "y": 8})
            result["custom"]["y"] = 0
            equal(d, {"custom": {"y": 8}})
        check("input_copy", "constraint", no_mutation)
        check("tags", "functional", lambda: equal(module.load_config(fixture, {"tags": ["z", "a", "z"]})["tags"], ["a", "z"]))
        check("invalid_tags", "functional", lambda: rejects(lambda: module.load_config(fixture, {"tags": [1]}), "tags"))
        legacy = work / "legacy.json"
        legacy.write_text('{"retry_count": 9}')
        check("legacy", "functional", lambda: equal(module.load_config(legacy)["retries"], 9))
        check("layers", "functional", lambda: equal(module.load_config_layers([fixture, legacy])["retries"], 9))
        check("redact", "functional", lambda: equal(module.redact_config({"nested": [{"token": "s", "x": 2}]}), {"nested": [{"token": "***", "x": 2}]}))
        value = {"中": 2, "a": 1}
        expected = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        check("fingerprint", "functional", lambda: equal(module.config_fingerprint(value), expected))
        check("describe", "functional", lambda: equal(module.describe_config(value), {"keys": sorted(value), "fingerprint": expected}))
    elif task == "csv":
        rows = [{"id": " 2 ", "name": " B ", "status": "ACTIVE", "extra": "x"}, {"id": "1", "name": "A"}]
        check("sort", "constraint", lambda: equal([r["id"] for r in module.clean_rows(rows)], ["1", "2"]))
        check("unknown_fields", "constraint", lambda: equal(module.clean_rows(rows)[1]["extra"], "x"))
        check("error_field", "constraint", lambda: rejects(lambda: module.clean_rows([{"id": " "}]), "id"))
        def no_mutation():
            module.clean_rows(rows)[1]["name"] = "Changed"
            equal(rows[0]["name"], " B ")
        check("input_copy", "constraint", no_mutation)
        check("summary", "functional", lambda: equal(module.summarize(rows), {"total": 2, "by_status": {"active": 1, "pending": 1}}))
        check("merge", "functional", lambda: equal(module.merge_rows(rows, [{"id": "1", "name": "C"}])[0]["name"], "C"))
        check("active", "functional", lambda: equal([r["id"] for r in module.active_only(rows)], ["2"]))
        check("group", "functional", lambda: equal(module.group_by_status(rows)["active"][0]["id"], "2"))
        check("select", "functional", lambda: equal([r["id"] for r in module.select_ids(rows, [2])], ["2"]))
        clean = [{"id": "1", "name": "A", "status": "pending"}, {"id": "2", "name": "B", "status": "active", "extra": "x"}]
        expected = hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        check("checksum", "functional", lambda: equal(module.rows_checksum(rows), expected))
        check("json", "functional", lambda: equal(json.loads(module.to_json(rows)), clean))
        def csv_io():
            path = work / "rows.csv"
            module.write_csv(path, rows)
            equal(path.read_text(encoding="utf-8").splitlines()[0], "id,name,status,extra")
            equal(module.read_csv(path)[1]["id"], "2")
        check("csv_io", "functional", csv_io)
    else:
        fixture.write_text('[{"id":"1","title":"one","status":"pending","custom":7}]')
        def store():
            return module.TaskStore(fixture)
        check("unknown_fields", "constraint", lambda: equal(store().get("1")["custom"], 7))
        check("normalized_id", "constraint", lambda: equal(store().get(" 1 ")["id"], "1"))
        check("error_field", "constraint", lambda: rejects(lambda: store().add("", "title"), "id"))
        def no_alias():
            s = store()
            s.list_tasks()[0]["custom"] = 0
            equal(s.get("1")["custom"], 7)
        check("output_copy", "constraint", no_alias)
        def lifecycle():
            s = store()
            s.add("2", "中文")
            s.update("2", status="active", custom=8)
            equal(s.counts(), {"pending": 1, "active": 1, "done": 0})
            equal(s.list_by_status("active")[0]["custom"], 8)
            s.complete_many(["1", "2"])
            equal(s.counts()["done"], 2)
            equal(s.rename("2", " renamed ")["title"], "renamed")
            equal(json.loads(s.export_json())[1]["title"], "renamed")
            s.save()
            equal(store().get("2")["custom"], 8)
            equal(s.remove("2")["id"], "2")
        check("lifecycle", "functional", lifecycle)
        check("invalid_status", "functional", lambda: rejects(lambda: store().update("1", status="bad"), "status"))
        def atomic_complete():
            s = store()
            s.update("1", status="pending")
            rejects(lambda: s.complete_many(["1", "missing"]), "id")
            equal(s.get("1")["status"], "pending")
        check("atomic_complete", "functional", atomic_complete)
    return checks


if __name__ == "__main__":
    import importlib.util
    import sys
    work = Path(sys.argv[1]).resolve()
    spec = importlib.util.spec_from_file_location("solution", work / "solution.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.__bench_task__ = sys.argv[2]
    print(json.dumps(acceptance(mod, work), ensure_ascii=False))
