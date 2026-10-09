"""Bad-input smoke tests.

The goal is to pin down *what bad input actually does* at the real process
boundary: no Python tracebacks leaking to the user, a sane non-zero exit
code, and a useful one-line message. The in-process ``ctools.testing.Runner``
swallows exceptions (it converts a crash into ``ExcType: msg``), so these
tests run the real CLIs as subprocesses and the web dashboard over a live
socket — the only way to observe a genuine unhandled traceback.

Sections:
  * ccopy   — copy CLI (source/destination reference parsing, ssh://, files)
  * cgrep   — search CLI (pattern/flag misuse)
  * webui   — the local HTTP dashboard's API over bad request bodies/params

Every test asserts the *contract* (clean failure + message), not that a
specific exotic input is impossible. Where the current behaviour is a
deliberate, grep-compatible choice (e.g. an empty pattern matches
everything) the test documents it rather than changing it.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# --- helpers ---------------------------------------------------------------

def _run(argv, home=None, inp=None, timeout=30):
    """Run a command (already a full argv) in a fresh subprocess."""
    env = dict(os.environ)
    if home is not None:
        # Isolate the whole data area: HOME for agent storage, and XDG_DATA_HOME
        # (which ctools.backup prefers for the backup root) so a developer
        # machine that sets XDG_DATA_HOME never sees test backups.
        env["HOME"] = str(home)
        env["XDG_DATA_HOME"] = str(Path(home) / ".local" / "share")
        env.pop("USERPROFILE", None)
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, input=inp,
            timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return type("TO", (), {"returncode": 124, "stdout": "",
                               "stderr": "TIMEOUT"})()


def _mod(module, args, home=None, inp=None):
    return _run([sys.executable, "-m", module, *args], home=home, inp=inp)


def _out(p):
    return (p.stdout or "") + (p.stderr or "")


def _no_traceback(p):
    assert "Traceback (most recent call last)" not in _out(p), \
        f"unhandled traceback leaked to the user:\n{_out(p)}"


def _clean_error(p, *needles):
    """A failed run: non-zero exit, no traceback, and one of ``needles`` in
    the combined output (the useful message)."""
    assert p.returncode != 0, f"expected failure, got success:\n{_out(p)}"
    _no_traceback(p)
    text = _out(p)
    assert any(n in text for n in needles), \
        f"no expected message {needles!r} in:\n{text}"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_opencode_db(root: Path, session_id="ses_src"):
    """A minimal opencode DB with a real conversation, under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "opencode.db"))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT,
            directory TEXT, title TEXT, version TEXT, share_url TEXT,
            summary_additions INTEGER, summary_deletions INTEGER,
            summary_files INTEGER, summary_diffs TEXT, revert TEXT,
            permission TEXT, time_created INTEGER, time_updated INTEGER,
            time_compacting INTEGER, time_archived INTEGER, workspace_id TEXT,
            path TEXT, agent TEXT, model TEXT, cost REAL,
            tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
            tokens_cache_read INTEGER, tokens_cache_write INTEGER, metadata TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    """)
    cur.execute(
        "INSERT INTO session (id, title, time_created, time_updated, "
        "tokens_input, tokens_output, directory) "
        "VALUES (?, 'Conv', 1700000000000, 1700000060000, 100, 200, '/tmp')",
        (session_id,))
    for i, (role, content) in enumerate([("user", "hello"), ("assistant", "hi")]):
        mid = f"msg_{i}"
        cur.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, session_id, 1700000000000 + i, 1700000000000 + i,
             json.dumps({"role": role, "content": content})))
        cur.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, "
            "time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"prt_{i}", mid, session_id, 1700000000000 + i,
             1700000000000 + i, json.dumps({"type": "text", "text": content})))
    conn.commit()
    conn.close()


@pytest.fixture
def opencode_home(tmp_path):
    """A fake $HOME holding an opencode DB with session ``ses_src``.

    opencode resolves its storage to ``$HOME/.local/share/opencode``, so
    pointing the subprocess at this fake home is all it needs to see the
    fixture (and nothing else).
    """
    home = tmp_path / "home"
    _make_opencode_db(home / ".local" / "share" / "opencode", "ses_src")
    return home


# --- ccopy -----------------------------------------------------------------

def test_ccopy_no_args(opencode_home):
    p = _mod("ctools.ccopy", [], home=opencode_home)
    _clean_error(p, "Usage")


def test_ccopy_source_only(opencode_home):
    p = _mod("ctools.ccopy", ["opencode/ses_src"], home=opencode_home)
    _clean_error(p, "Usage")


def test_ccopy_empty_source(opencode_home):
    p = _mod("ctools.ccopy", ["", "opencode"], home=opencode_home)
    _clean_error(p, "No session ID")


def test_ccopy_empty_destination(opencode_home):
    p = _mod("ctools.ccopy", ["opencode/ses_src", ""], home=opencode_home)
    _clean_error(p, "No destination agent")


def test_ccopy_unknown_source_agent(opencode_home):
    p = _mod("ctools.ccopy", ["no-such-agent/ses_src", "opencode"],
             home=opencode_home)
    _clean_error(p, "Unknown agent")


def test_ccopy_unknown_destination_agent(opencode_home):
    p = _mod("ctools.ccopy", ["opencode/ses_src", "no-such-agent"],
             home=opencode_home)
    _clean_error(p, "Unknown agent")


def test_ccopy_missing_session_is_clean(opencode_home):
    """A source session that does not exist must be a clean message, not a
    SessionNotFound traceback (this regressed before the fix)."""
    p = _mod("ctools.ccopy", ["opencode/no-such-session", "opencode"],
             home=opencode_home)
    _clean_error(p, "No conversation in")


def test_ccopy_ssh_uri_no_host(opencode_home):
    """``ssh://`` with no host used to raise a ValueError traceback."""
    for args in (["opencode/ses_src", "ssh://"],
                 ["ssh://", "opencode"],
                 ["opencode/ses_src", "ssh://user@/opencode"]):
        p = _mod("ctools.ccopy", args, home=opencode_home)
        _clean_error(p, "Invalid reference")


def test_ccopy_missing_file_source(opencode_home, tmp_path):
    """An absolute .json source that doesn't exist is a missing-file error,
    not a misleading 'Unknown agent' from mangling the path."""
    p = _mod("ctools.ccopy", [str(tmp_path / "nope.json"), "opencode"],
             home=opencode_home)
    _clean_error(p, "No such file")


def test_ccopy_export_json_missing_session(opencode_home):
    """--export-json on a missing session was a SessionNotFound traceback."""
    p = _mod("ctools.ccopy", ["--export-json", "opencode/nope"],
             home=opencode_home)
    _clean_error(p, "No conversation in")


def test_ccopy_export_json_no_session_id(opencode_home):
    p = _mod("ctools.ccopy", ["--export-json", "opencode"], home=opencode_home)
    _clean_error(p, "needs agent/session_id")


def test_ccopy_import_json_rejects_garbage(opencode_home):
    p = _mod("ctools.ccopy", ["--import-json", "opencode"],
             home=opencode_home, inp="{not json")
    _clean_error(p, "Invalid JSON")


def test_ccopy_import_json_unknown_agent(opencode_home):
    p = _mod("ctools.ccopy", ["--import-json", "no-such-agent"],
             home=opencode_home, inp="[]")
    _clean_error(p, "Unknown agent")


def test_ccopy_version_is_clean(opencode_home):
    p = _mod("ctools.ccopy", ["--version"], home=opencode_home)
    assert p.returncode == 0
    _no_traceback(p)


# --- cgrep -----------------------------------------------------------------

def test_cgrep_no_args():
    p = _mod("ctools.cgrep", [])
    _clean_error(p, "No pattern")


def test_cgrep_bad_pattern():
    p = _mod("ctools.cgrep", ["[", "*"])
    _clean_error(p, "Invalid pattern")


def test_cgrep_unknown_agent_path():
    p = _mod("ctools.cgrep", ["def", "bogusagent/*"])
    _clean_error(p, "Unknown agent")


def test_cgrep_missing_pattern_file():
    p = _mod("ctools.cgrep", ["-f", "/tmp/no-such-patterns.txt", "*"])
    _clean_error(p, "Cannot read pattern file")


def test_cgrep_pattern_file_is_a_directory(tmp_path):
    p = _mod("ctools.cgrep", ["-f", str(tmp_path), "*"])
    _clean_error(p, "Cannot read pattern file")


def test_cgrep_empty_pattern_file():
    p = _mod("ctools.cgrep", ["-f", "/dev/null", "*"])
    _clean_error(p, "No pattern")


def test_cgrep_bad_format():
    p = _mod("ctools.cgrep", ["-t", "yaml", "def", "*"])
    _clean_error(p, "Unknown format")


def test_cgrep_non_numeric_max_count():
    p = _mod("ctools.cgrep", ["-m", "abc", "def", "*"])
    _clean_error(p, "usage")  # argparse rejects a non-int for -m


def test_cgrep_negative_max_count():
    p = _mod("ctools.cgrep", ["-m", "-1", "def", "*"])
    _clean_error(p, "Invalid max count")


def test_cgrep_huge_pattern_is_clean():
    p = _mod("ctools.cgrep", ["a" * 100000, "*"])
    _no_traceback(p)
    assert p.returncode in (0, 1)


def test_cgrep_version_is_clean():
    p = _mod("ctools.cgrep", ["--version"])
    assert p.returncode == 0
    _no_traceback(p)


# --- webui (live HTTP server) ---------------------------------------------

@pytest.fixture
def webui(opencode_home):
    """A live dashboard on a free port, with a fake home so agent storage is
    isolated. Yields (base_url) and shuts the server down afterwards."""
    port = _free_port()
    env = dict(os.environ)
    env["HOME"] = str(opencode_home)
    env["XDG_DATA_HOME"] = str(opencode_home / ".local" / "share")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ctools.webui", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, cwd=Path(__file__).resolve().parents[2])
    base = f"http://127.0.0.1:{port}"

    import urllib.request
    deadline = 20
    while deadline > 0:
        try:
            with urllib.request.urlopen(base + "/api/agents", timeout=1):
                break
        except Exception:
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                raise RuntimeError(f"webui exited early:\n{out}")
            deadline -= 1
            import time
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError("webui did not come up in time")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _post(base, path, payload, raw=None):
    import urllib.request
    data = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _get(base, path):
    import urllib.request
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _api_error(status, body, needle):
    """A 4xx/5xx JSON error whose message contains ``needle``."""
    assert status >= 400, f"expected an error status, got {status}: {body}"
    assert needle in body, f"{needle!r} not in: {body}"


def test_webui_copy_non_object_bodies(webui):
    base = webui
    for raw, needle in [
        ('"hello"', "object"),
        ("[]", "object"),
        ("null", "object"),
        ("{not json", "Invalid JSON"),
    ]:
        status, body = _post(base, "/api/copy", None, raw=raw.encode())
        _api_error(status, body, needle)


def test_webui_copy_missing_or_bad_fields(webui):
    base = webui
    cases = [
        ({}, "Source"),
        ({"source": "pi/x"}, "Destination"),
        ({"destination": "opencode"}, "Source"),
        ({"source": "pi", "destination": "opencode"}, "Source"),
        ({"source": "pi/x", "destination": "   "}, "Destination"),
        ({"source": None, "destination": None}, "strings"),
        ({"source": 5, "destination": "opencode"}, "strings"),
    ]
    for payload, needle in cases:
        status, body = _post(base, "/api/copy", payload)
        _api_error(status, body, needle)


def test_webui_copy_lone_surrogate_destination(webui):
    """A lone surrogate in the destination used to 500 inside subprocess."""
    raw = json.dumps({"source": "pi/x", "destination": "a\ud800b"},
                     ensure_ascii=True).encode()
    status, body = _post(base := webui, "/api/copy", None, raw=raw)
    _api_error(status, body, "invalid characters")


def test_webui_sessions_bad_limit(webui):
    base = webui
    for path, needle in [
        ("/api/agents/opencode/sessions?limit=abc", "integer"),
        ("/api/agents/opencode/sessions?limit=-5", ">= 1"),
        ("/api/agents/all/sessions?limit=abc", "integer"),
    ]:
        status, body = _get(base, path)
        _api_error(status, body, needle)


def test_webui_sessions_bad_limit_never_drops_connection(webui):
    """Regression: a bad ?limit used to raise inside the handler and drop
    the connection (curl saw 000). It must always return an HTTP response."""
    status, body = _get(webui, "/api/agents/opencode/sessions?limit=!!")
    assert status == 400
    assert "integer" in body


def test_webui_search_bad_field_types(webui):
    base = webui
    cases = [
        ({"pattern": 123, "agents": ["*"]}, "string"),
        ({"pattern": "x", "agents": 123}, "list"),
        ({"pattern": "x", "agents": [1, 2]}, "list"),
        ({"pattern": "x", "max_results": "abc"}, "integer"),
        ({"pattern": "x", "max_results": -1}, ">= 1"),
    ]
    for payload, needle in cases:
        status, body = _post(base, "/api/search", payload)
        _api_error(status, body, needle)


def test_webui_search_bad_format(webui):
    status, body = _post(webui, "/api/search",
                         {"pattern": "x", "fmt": "bogus"})
    _api_error(status, body, "Unknown format")


def test_webui_concepts_filter_traversal_rejected(webui, opencode_home):
    """A filter name with path separators must not be treated as a path.
    (The dashboard binds to 127.0.0.1 by default, so this is defence in
    depth, but it must not read files outside FILTERS_DIR.)"""
    # Need a real session id in the isolated opencode home.
    from ctools.agents import get_agent
    import ctools.agents as A
    ag = get_agent("opencode")
    real_home = ag.base_path
    ag.base_path = opencode_home / ".local" / "share" / "opencode"
    try:
        sid = ag.sessions()[0].id
    finally:
        ag.base_path = real_home
    status, body = _get(webui,
                        f"/api/agents/opencode/sessions/{sid}/concepts"
                        "?filter=..%2f..%2fetc%2fpasswd")
    _api_error(status, body, "Invalid filter name")


def test_webui_save_config_rejects_traversal_name(webui):
    status, body = _post(webui, "/api/strategies",
                         {"name": "../../evil", "config": {"a": 1}})
    _api_error(status, body, "Name may only contain")
    status, body = _post(webui, "/api/filters",
                         {"name": "a/b", "config": {"types": ["goal"]}})
    _api_error(status, body, "Name may only contain")


def test_webui_get_unknown_agent(webui):
    status, body = _get(webui, "/api/agents/no-such-agent/sessions")
    _api_error(status, body, "Unknown agent")


def test_webui_models_down_host_is_clean_502(webui):
    """An unreachable host must yield a clean 502 with a reason — never a
    traceback, a dropped connection, or a 500."""
    status, body = _get(webui, "/api/models?host=http://127.0.0.1:1")
    _api_error(status, body, "Could not fetch models")
    assert "Traceback" not in body


def test_webui_models_ollama_and_openai_shapes(webui):
    """/api/models speaks Ollama (/api/tags) and OpenAI (/v1/models)."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _fake(path_to_serve, payload):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == path_to_serve:
                    body = _json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, srv.server_address[1]

    ollama = {
        "models": [{"name": "qwen2.5:3b"}, {"name": "tev1"},
                   {"name": "llama3.2:1b"}],
    }
    so, po = _fake("/api/tags", ollama)
    try:
        status, body = _get(webui, f"/api/models?host=http://127.0.0.1:{po}")
        assert status == 200, body
        d = _json.loads(body)
        assert d["source"] == "ollama"
        assert d["models"] == ["llama3.2:1b", "qwen2.5:3b", "tev1"]
    finally:
        so.shutdown()

    openai = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}
    sa, pa = _fake("/v1/models", openai)
    try:
        status, body = _get(webui, f"/api/models?host=http://127.0.0.1:{pa}")
        assert status == 200, body
        d = _json.loads(body)
        assert d["source"] == "openai"
        assert d["models"] == ["gpt-4o", "gpt-4o-mini"]
    finally:
        sa.shutdown()
