"""Restore a historical pre-task snapshot without touching the original workspace."""
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tokenize


def redact_source(text):
    """Keep Python syntax intact while masking credential-like string literals."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        previous = []
        for index, token in enumerate(tokens):
            if token.type == tokenize.STRING:
                recent = " ".join(previous[-4:])
                if re.search(r"(?i)api[_-]?key|password|secret|authorization", recent) or re.search(
                    r"sk-[A-Za-z0-9_-]{12,}", token.string
                ):
                    tokens[index] = token._replace(string='"REDACTED_FOR_BENCHMARK"')
            if token.type not in {tokenize.NL, tokenize.COMMENT}:
                previous.append(token.string)
        return tokenize.untokenize(tokens)
    except (tokenize.TokenError, IndentationError):
        raise ValueError("Cannot safely sanitize snapshot source") from None


def restore(snapshot_file, trace_file, workspace):
    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    repository = snapshot_file.parent.parent / "objects.git"
    revision = snapshot["before_revision"]
    def git(*args):
        return subprocess.check_output(["git", f"--git-dir={repository}", *args])
    paths = git("ls-tree", "-rz", revision).split(b"\0")
    redacted, copied = [], []
    for entry in paths:
        if not entry:
            continue
        metadata, encoded_path = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        relative = encoded_path.decode("utf-8")
        if mode not in {"100644", "100755"} or kind != "blob":
            continue
        # No original secrets, runtime data, editor settings or external links.
        parts = Path(relative).parts
        if any(p.startswith(".") for p in parts) or relative.endswith((".pem", ".key")):
            continue
        target = (workspace / relative).resolve()
        if not target.is_relative_to(workspace.resolve()):
            raise ValueError("Snapshot path escapes fixture")
        content = git("cat-file", "blob", object_id).decode("utf-8")
        clean = redact_source(content) if relative.endswith(".py") else re.sub(
            r"sk-[A-Za-z0-9_-]{12,}", "REDACTED_FOR_BENCHMARK", content,
        )
        if content != clean:
            redacted.append(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(clean, encoding="utf-8")
        copied.append(relative)
    if (workspace / "tests" / "todo_api").exists():
        raise ValueError("Expected a pre-implementation snapshot, but Todo code already exists")
    prompt = None
    for line in trace_file.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "runtime_request" and event["data"].get("method") == "task.submit":
            params = event["data"]["params"]
            prompt = params.get("prompt") or params.get("message") or params.get("input")
            break
    if not isinstance(prompt, str) or "Todo" not in prompt:
        raise ValueError("Original Todo prompt not found in trace")
    hooks = workspace.parent / "disabled-hooks"
    hooks.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workspace), "-c", f"core.hooksPath={hooks}",
                    "-c", "user.name=LatencyBenchmark", "-c", "user.email=benchmark@localhost",
                    "commit", "--quiet", "-m", "Historical sanitized pre-task fixture"], check=True)
    return prompt, {"source_revision": revision, "files": copied, "redacted_files": redacted,
                    "excluded": "dotfiles/directories, key files, symlinks; source literals sanitized"}


ACCEPTANCE = '''import sys, importlib, unittest
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
class TodoAcceptance(unittest.TestCase):
    def setUp(self):
        for name in list(sys.modules):
            if name in ("tests", "app") or name.startswith(("tests.", "app.")): del sys.modules[name]
        module=importlib.import_module(sys.argv[2])
        app=module.create_app() if hasattr(module,"create_app") else module.app
        self.c=TestClient(app)
    def create(self, **kw):
        r=self.c.post("/todos",json={"title":"task",**kw}); self.assertIn(r.status_code,(200,201))
        return r.json()
    def items(self, r):
        self.assertEqual(r.status_code,200); data=r.json()
        return data if isinstance(data,list) else data["items"]
    def test_fields(self):
        t=self.create(description="hello")
        self.assertTrue(set(["id","title","description","status","priority","created_at","updated_at"])<=t.keys())
        self.assertEqual(t["description"],"hello")
    def test_title(self):
        for title in ("","   ",None):
            self.assertIn(self.c.post("/todos",json={"title":title}).status_code,(400,422))
    def test_enums(self):
        for key in ("status","priority"):
            self.assertIn(self.c.post("/todos",json={"title":"x",key:"invalid"}).status_code,(400,422))
    def test_get(self):
        t=self.create(); self.assertEqual(self.c.get(f"/todos/{t['id']}").json()["title"],"task")
    def test_list(self):
        self.create(title="a");self.create(title="b")
        self.assertEqual(len(self.items(self.c.get("/todos"))),2)
    def test_status_filter(self):
        self.create(status="pending");self.create(status="done")
        items=self.items(self.c.get("/todos",params={"status":"done"}))
        self.assertEqual(len(items),1);self.assertEqual(items[0]["status"],"done")
    def test_priority_filter(self):
        self.create(priority="low");self.create(priority="high")
        items=self.items(self.c.get("/todos",params={"priority":"high"}))
        self.assertEqual(len(items),1);self.assertEqual(items[0]["priority"],"high")
    def test_update(self):
        t=self.create();r=self.c.put(f"/todos/{t['id']}",json={"title":"changed","description":"new","status":"doing","priority":"high"})
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()["title"],"changed")
        self.assertEqual(self.c.get(f"/todos/{t['id']}").json()["status"],"doing")
    def test_delete(self):
        t=self.create(); self.assertIn(self.c.delete(f"/todos/{t['id']}").status_code,(200,204))
        self.assertEqual(self.c.get(f"/todos/{t['id']}").status_code,404)
    def test_missing(self):
        self.assertEqual(self.c.get("/todos/99999").status_code,404)
        self.assertEqual(self.c.put("/todos/99999",json={"title":"x"}).status_code,404)
        self.assertEqual(self.c.delete("/todos/99999").status_code,404)
    def test_pagination(self):
        for i in range(5):self.create(title=str(i))
        a=self.items(self.c.get("/todos",params={"page":1,"page_size":2}))
        b=self.items(self.c.get("/todos",params={"page":2,"page_size":2}))
        self.assertEqual(len(a),2);self.assertEqual(len(b),2)
        self.assertFalse({x["id"] for x in a}&{x["id"] for x in b})
    def test_invalid_pagination(self):
        for key in ("page","page_size"):
            for value in (0,-1,"abc","1.5"):
                self.assertIn(self.c.get("/todos",params={key:value}).status_code,(400,422))
unittest.main(argv=["acceptance"],verbosity=2)
'''


def validate(workspace, private=False):
    # The prompt specifies HTTP behavior, not a Python import path or test framework.
    # Support both historical and conventional layouts without altering assertions.
    candidates = ("tests.todo_api.app", "app.main")
    app_module = next((name for name in candidates
                       if (workspace / (name.replace(".", "/") + ".py")).is_file()), candidates[0])
    command = ([sys.executable, "-I", "-c", ACCEPTANCE, str(workspace), app_module] if private else
               [sys.executable, "-m", "pytest", "tests", "-q", "-o", "addopts="])
    environment = {k.upper(): v for k, v in __import__("os").environ.items()
                   if k.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "PATH", "PATHEXT", "COMSPEC"}}
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(command, env=environment,
                            cwd=workspace, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    output = result.stdout + result.stderr
    count = re.search(r"Ran (\d+) tests?" if private else r"(\d+) passed", output)
    return {"exit_code": result.returncode, "test_count": int(count[1]) if count else 0,
            "app_module": app_module if private else None,
            "output": output[-18000:]}
