from __future__ import annotations

import json
import sys
from pathlib import Path


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\n", b"\r\n"}:
            break
        name, value = line.decode("ascii").split(":", 1)
        headers[name.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])).decode("utf-8"))


def write_message(message):
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def main():
    log_path = Path(sys.argv[1])
    ignored_uri = sys.argv[2]
    if len(sys.argv) > 3 and sys.argv[3] == "hang-initialize":
        import time

        read_message()
        time.sleep(60)
        return
    messages = []
    opened_uri = ""
    request = read_message()
    messages.append(request)
    write_message(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "capabilities": {"positionEncoding": "utf-16"},
                "serverInfo": {"name": "fake-python-lsp", "version": "1.2.3"},
            },
        }
    )
    while True:
        request = read_message()
        if request is None:
            break
        messages.append(request)
        method = request.get("method")
        if method == "textDocument/didOpen":
            opened_uri = request["params"]["textDocument"]["uri"]
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": 700,
                    "method": "workspace/applyEdit",
                    "params": {"edit": {"changes": {opened_uri: []}}},
                }
            )
            write_message(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": ignored_uri,
                        "diagnostics": [
                            {
                                "range": {
                                    "start": {"line": 0, "character": 0},
                                    "end": {"line": 0, "character": 1},
                                },
                                "severity": 1,
                                "message": "must be ignored",
                            }
                        ],
                    },
                }
            )
            write_message(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": opened_uri,
                        "version": 1,
                        "diagnostics": [
                            {
                                "range": {
                                    "start": {"line": 2, "character": 4},
                                    "end": {"line": 2, "character": 8},
                                },
                                "severity": 1,
                                "source": "fake",
                                "code": "FAKE001",
                                "message": "fake diagnostic",
                            }
                        ],
                    },
                }
            )
        elif method == "shutdown":
            write_message({"jsonrpc": "2.0", "id": request["id"], "result": None})
        elif method == "exit":
            break
    log_path.write_text(json.dumps(messages), encoding="utf-8")


if __name__ == "__main__":
    main()
