"""Smoke-compare the long-running verbs on bob: the baseline's ctools-web /
ctools-mcp (~/ctools) against the current tree's `ctools web` / `ctools mcp`
(~/ctools-current). Run on bob with the pipx venv python; bob-smoke.sh does it.

Servers can't go through the line-diff sweep, so this drives each one with the
same requests and diffs the responses. Exit 0 iff every response matched.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
BASE, CUR = f"{HOME}/ctools", f"{HOME}/ctools-current"
ENV = dict(os.environ, PYTHONPATH=".")

GETS = [
    "/",
    "/api/agents",
    "/api/agents/pi/sessions",
    "/api/agents/goose/sessions",
    "/api/agents/opencode/sessions",
    "/api/agents/goose/sessions/20260205_4",
    "/api/agents/claude-code/sessions/718c4267-0ff2-4737-8f46-658d56450d47",
    "/api/agents/nosuchagent/sessions",
    "/api/strategies",
    "/api/filters",
    "/nosuchpage",
]
SEARCH = {"pattern": "plugshare", "agents": ["claude-code"], "max_results": 20}

MCP_REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "bob-smoke", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "list_agents", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "list_sessions", "arguments": {"agent": "goose"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "export_session",
                "arguments": {"agent": "goose", "session_id": "20260205_4"}}},
]


def fetch(port, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, data = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, data = e.code, e.read()
    text = data.decode("utf-8", "replace")
    # The page carries a random per-process shutdown token.
    text = re.sub(r'const SHUTDOWN_TOKEN = "[^"]*";', 'const SHUTDOWN_TOKEN = "<token>";', text)
    return f"{status}\n{text}"


def web_responses(cwd, argv, port):
    proc = subprocess.Popen([sys.executable, "-m", *argv, "--port", str(port), "--no-logo"],
                            cwd=cwd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        for _ in range(150):
            try:
                fetch(port, "/api/agents")
                break
            except OSError:
                if proc.poll() is not None:
                    return {"<startup>": proc.stdout.read()}
                time.sleep(0.1)
        out = {p: fetch(port, p) for p in GETS}
        out["POST /api/search"] = fetch(port, "/api/search", SEARCH)
        return out
    finally:
        proc.terminate()
        proc.wait(10)


def mcp_responses(cwd, argv):
    stdin = "".join(json.dumps(m) + "\n" for m in MCP_REQUESTS)
    # mcp is an optional extra; MCP_PYTHON points at an interpreter that has it.
    python = os.environ.get("MCP_PYTHON") or sys.executable
    proc = subprocess.Popen([python, "-m", *argv], cwd=cwd, env=ENV, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # One request at a time: closing stdin early ends the session before replies.
    lines = []
    for msg in MCP_REQUESTS:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if "id" in msg:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
    proc.stdin.close()
    rest, err = proc.communicate(timeout=60)
    # The mcp library logs with a wall-clock prefix.
    err = re.sub(r"^\[\d\d/\d\d/\d\d \d\d:\d\d:\d\d\]", "[<time>]", err, flags=re.M)
    return {"stdout": "".join(lines) + rest, "stderr": err, "rc": str(proc.returncode)}


def compare(label, a, b):
    bad = [k for k in a.keys() | b.keys() if a.get(k) != b.get(k)]
    for k in sorted(a):
        print(f"  {'FAIL' if k in bad else 'ok  '} {label} {k}  ({len(a[k])} bytes)")
    for k in sorted(bad):
        print(f"    --- {k}\n    baseline: {a.get(k, '<missing>')[:300]!r}\n"
              f"    current:  {b.get(k, '<missing>')[:300]!r}")
    return not bad


def main():
    ok = compare("web",
                 web_responses(BASE, ["ctools.webui"], 18765),
                 web_responses(CUR, ["ctools", "web"], 18766))
    mcp_a = mcp_responses(BASE, ["ctools.ctools_mcp"])
    mcp_b = mcp_responses(CUR, ["ctools", "mcp"])
    replies = mcp_a["stdout"].count('"jsonrpc"')
    print(f"  mcp: {replies} JSON-RPC replies from the baseline")
    ok &= replies == 5
    ok &= compare("mcp", mcp_a, mcp_b)
    print("all matched" if ok else "MISMATCH")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
