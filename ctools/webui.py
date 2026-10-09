#!/usr/bin/env python3
"""
webui - ctools dashboard in your browser.

A tiny, dependency-free (stdlib only) HTTP server that wraps the ctools
library and serves a single-page dashboard. Works the same on macOS, Windows
and Linux: open http://127.0.0.1:8765 and get an overview of every agent
installed, full-text search across all of them, a session browser, a
conversation viewer, concept extraction, and one-click conversation copy.

The server binds to 127.0.0.1 by default so it is only reachable from the
local machine; pass --host 0.0.0.0 only if you know why you want that.

    python -m ctools.webui           # start on 127.0.0.1:8765
    ctools-webui --port 9000
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ctools import __version__
from ctools.agents import (
    AGENT_CLASSES, REGISTRY, Agent, AgentError,
    get_agent, get_resume_command, text_of,
)
# Some ctools submodules may be mid-refactor and fail to import (e.g. the
# in-progress cli.py version_option signature change). Keep the dashboard up
# and degrade those features gracefully instead of refusing to start.
try:
    from ctools.cextract import session_concepts
except Exception:  # pragma: no cover - depends on repo state
    session_concepts = None
try:
    from ctools.ccopy import copy_conversation
except Exception:  # pragma: no cover - depends on repo state
    copy_conversation = None
try:
    from ctools.cgrep import _compile_pattern
except Exception:  # pragma: no cover - depends on repo state
    _compile_pattern = None
from ctools.cdu import _session_tokens, format_tokens, get_session_tokens
from ctools.lib import get_formatter


# ---------------------------------------------------------------------------
# Helpers

def _msg_brief(m) -> dict:
    return {
        "role": m.role,
        "content": m.content,
        "length": len(m.content),
    }


def _agent_brief(agent: Agent) -> dict:
    return {
        "name": agent.name,
        "description": agent.description,
        "storage": agent.storage_format,
        "installed": agent.exists(),
        "base_path": str(agent.base_path),
    }


def _session_brief(s) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "ctime": s.ctime.isoformat() if s.ctime else None,
        "mtime": s.mtime.isoformat() if s.mtime else None,
        "size": s.size,
        "path": s.path,
        "model": s.model,
        "message_count": s.message_count,
        "tokens": _session_tokens(s),
    }


def _do_search(pattern: str, agents, ignore_case: bool, max_results: int, fmt: str = "default"):
    if _compile_pattern is None:
        return None, "Search unavailable (ctools.cgrep failed to import)."
    try:
        compiled = _compile_pattern(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return None, f"Invalid pattern: {e}"

    if agents == ["*"]:
        targets = list(REGISTRY.values())
    else:
        targets = []
        for name in agents:
            agent = get_agent(name)
            if agent is None:
                return None, f"Unknown agent: {name}"
            targets.append(agent)

    matches = []
    per_agent = {}
    errors = []
    for agent in targets:
        if not agent.exists():
            continue
        try:
            sessions = agent.sessions()
        except AgentError as e:
            errors.append(f"{agent.name}: {e}")
            continue
        for session in sessions:
            try:
                lines = agent.lines(session.id)
            except AgentError:
                continue
            for line_num, line in lines:
                if compiled.search(line):
                    matches.append({
                        "agent": agent.name,
                        "session_id": session.id,
                        "line_num": line_num,
                        "line": line[:1000],
                    })
                    per_agent[agent.name] = per_agent.get(agent.name, 0) + 1
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    result = {"matches": matches, "total": len(matches), "per_agent": per_agent, "errors": errors}
    if fmt != "default":
        try:
            formatter = get_formatter(fmt)
        except ValueError as e:
            return None, str(e)
        from ctools.agents import Match as _M
        result["formatted"] = formatter.format_matches(
            [_M(agent=m["agent"], session_id=m["session_id"],
                line_num=m["line_num"], line=m["line"]) for m in matches])
    return result, None


# ---------------------------------------------------------------------------
# HTTP handler

class Handler(BaseHTTPRequestHandler):
    server_version = f"ctools-webui/{__version__}"

    # --- plumbing ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str):
        self._json({"error": message}, code)

    # --- model discovery ---------------------------------------------------

    @staticmethod
    def _http_get_json(url: str, headers: dict, timeout: float = 4.0):
        """GET ``url`` and return ``(status, parsed_json_or_text, err)``.

        Never raises for the common failure modes (connection refused, DNS,
        timeout, bad JSON) — those come back as a non-2xx status plus an
        error string so the caller can fall through to the next scheme.
        """
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.getcode()
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, None, f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", None) or e
            return 0, None, str(reason)
        except Exception as e:  # noqa: BLE001 - network surprises
            return 0, None, f"{type(e).__name__}: {e}"
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = raw
        return status, parsed, None

    def _get_models(self):
        """List the models a server at ``?host=`` advertises.

        Tries Ollama's native ``/api/tags`` first (returns model *names*),
        then the OpenAI-compatible ``/v1/models`` (returns model *ids*). An
        optional ``api_key`` is sent as a Bearer token for OpenAI-style
        servers. Returns ``{host, source, models: [...]}`` or a 400/502 with
        a useful reason.
        """
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else {})
        host = (q.get("host", ["http://localhost:11434"])[0] or "").strip().rstrip("/")
        api_key = (q.get("api_key", [""])[0] or "").strip()
        if not host:
            return self._error(400, "No host given.")
        if host not in ("http://", "https://") and "://" not in host:
            host = "http://" + host

        # 1) Ollama native
        status, data, err = self._http_get_json(f"{host}/api/tags", {}, timeout=3.0)
        if status == 200 and isinstance(data, dict) and isinstance(data.get("models"), list):
            names = [m.get("name") or m.get("model") for m in data["models"] if isinstance(m, dict)]
            names = sorted({n for n in names if n})
            return self._json({"host": host, "source": "ollama", "models": names})

        # 2) OpenAI-compatible
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        status2, data2, err2 = self._http_get_json(f"{host}/v1/models", headers, timeout=4.0)
        if status2 == 200 and isinstance(data2, dict) and isinstance(data2.get("data"), list):
            ids = [m.get("id") for m in data2["data"] if isinstance(m, dict)]
            ids = sorted({i for i in ids if i})
            return self._json({"host": host, "source": "openai", "models": ids})

        # Neither worked: report the most useful reason we have.
        reasons = []
        if err:
            reasons.append(f"ollama /api/tags: {err}")
        if err2:
            reasons.append(f"openai /v1/models: {err2}")
        detail = "; ".join(reasons) or "no model list returned"
        return self._error(502, f"Could not fetch models from {host}. {detail}")

    def _path(self) -> tuple:
        parts = [p for p in self.path.split("?", 1)[0].strip("/").split("/") if p]
        return parts

    def log_message(self, *args):
        pass  # keep the terminal quiet

    # --- GET ---------------------------------------------------------------

    def do_GET(self):
        # A top-level guard so a bug in any endpoint returns a clean 500
        # instead of dropping the connection (curl would see nothing) and
        # leaving a traceback in the server log.
        try:
            self._do_GET()
        except Exception as e:  # noqa: BLE001 - last-resort handler guard
            try:
                self._error(500, f"Internal error: {type(e).__name__}: {e}")
            except Exception:
                pass

    def _do_GET(self):
        parts = self._path()
        if not parts:
            return self._send(200, INDEX_HTML.encode("utf-8"),
                              "text/html; charset=utf-8")

        if parts[0] == "api":
            return self.api_get(parts[1:])
        return self._error(404, "Not found")

    def api_get(self, parts):
        if parts == ["models"]:
            return self._get_models()

        if parts == ["agents"]:
            out = []
            for brief in (_agent_brief(a) for a in REGISTRY.values()):
                if not brief["installed"]:
                    out.append(brief)
                    continue
                agent = get_agent(brief["name"])
                try:
                    sessions = agent.sessions()
                except AgentError:
                    sessions = []
                brief["session_count"] = len(sessions)
                brief["total_tokens"] = sum(_session_tokens(s) for s in sessions)
                brief["total_msgs"] = sum(s.message_count or 0 for s in sessions)
                out.append(brief)
            return self._json({"version": __version__, "agents": out})

        if len(parts) == 3 and parts[0] == "agents" and parts[2] == "sessions":
            name = parts[1]
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else {})
            sort = (q.get("sort", ["time"])[0])
            try:
                limit = int(q.get("limit", ["200"])[0])
            except (TypeError, ValueError):
                return self._error(400, "'limit' must be an integer")
            if limit < 1:
                return self._error(400, "'limit' must be >= 1")

            if name == "all":
                # Aggregate sessions across every installed agent.
                merged = []
                for brief in (_agent_brief(a) for a in REGISTRY.values()):
                    if not brief["installed"]:
                        continue
                    agent = get_agent(brief["name"])
                    try:
                        sessions = agent.sessions()
                    except AgentError:
                        continue
                    for s in sessions:
                        sb = _session_brief(s)
                        sb["agent"] = agent.name
                        merged.append(sb)

                def _sort_key(sb):
                    if sort == "size":
                        return sb["size"] or 0
                    if sort == "tokens":
                        return sb["tokens"] or 0
                    m = sb["mtime"] or sb["ctime"]
                    return m or ""
                merged.sort(key=_sort_key, reverse=True)
                return self._json({
                    "agent": "all",
                    "sessions": merged[:limit],
                    "total": len(merged),
                })

            agent = get_agent(name)
            if agent is None:
                return self._error(404, f"Unknown agent: {name}")
            if not agent.exists():
                return self._json({"agent": name, "sessions": []})
            try:
                sessions = agent.sessions()
            except AgentError as e:
                return self._error(500, str(e))
            if sort == "size":
                sessions.sort(key=lambda s: s.size, reverse=True)
            elif sort == "tokens":
                sessions.sort(key=lambda s: _session_tokens(s), reverse=True)
            else:
                sessions.sort(key=lambda s: s.mtime or s.ctime or datetime.min,
                              reverse=True)
            return self._json({
                "agent": agent.name,
                "sessions": [_session_brief(s) for s in sessions[:limit]],
                "total": len(sessions),
            })

        if len(parts) == 4 and parts[0] == "agents" and parts[2] == "sessions":
            agent = get_agent(parts[1])
            if agent is None:
                return self._error(404, f"Unknown agent: {parts[1]}")
            sid = urllib.parse.unquote(parts[3])
            try:
                messages = agent.messages(sid)
            except AgentError as e:
                return self._error(404, str(e))
            if not messages:
                return self._error(404, f"Session not found: {agent.name}/{sid}")
            try:
                tokens = get_session_tokens(agent.name, sid)
            except Exception:
                tokens = {}
            formatter = None
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else {})
            fmt = q.get("fmt", ["default"])[0]
            if fmt != "default":
                try:
                    formatter = get_formatter(fmt)
                except ValueError as e:
                    return self._error(400, str(e))
            result = {
                "agent": agent.name,
                "session_id": sid,
                "messages": [_msg_brief(m) for m in messages],
                "tokens": tokens,
                "resume": get_resume_command(agent.name, sid),
            }
            if formatter is not None:
                result["formatted"] = formatter.format_session_export(messages, sid, agent.name)
            return self._json(result)

        if len(parts) == 5 and parts[0] == "agents" and parts[2] == "sessions" \
                and parts[4] == "concepts":
            agent = get_agent(parts[1])
            if agent is None:
                return self._error(404, f"Unknown agent: {parts[1]}")
            sid = urllib.parse.unquote(parts[3])
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else {})
            concept_type = q.get("type", [""])[0]
            strategy = q.get("strategy", [""])[0] or None
            filter_name = q.get("filter", [""])[0] or None
            filter_config = None
            if filter_name:
                # Validate like _save_config does so a traversal name can't
                # read a file outside FILTERS_DIR.
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", filter_name):
                    return self._error(400, "Invalid filter name")
                from ctools.filterlib import FILTERS_DIR
                fp = FILTERS_DIR / f"{filter_name}.json"
                if fp.exists():
                    try:
                        filter_config = fp.read_text(encoding="utf-8")
                    except OSError:
                        filter_config = None
            if session_concepts is None:
                return self._error(503, "Concept extraction unavailable (ctools.cextract failed to import).")
            try:
                concepts = session_concepts(agent, sid, strategy, filter_config)
            except AgentError as e:
                return self._error(404, str(e))
            except SystemExit as e:
                return self._error(400, f"{e}")
            except Exception as e:
                return self._error(500, f"Concept extraction failed: {e}")
            if concept_type:
                concepts = [c for c in concepts if c.get("type") == concept_type]
            return self._json({"agent": agent.name, "session_id": sid,
                               "concepts": concepts})

        # --- strategies & filters -----------------------------------------
        if parts == ["strategies"]:
            from ctools.strategy import STRATEGIES_DIR
            names = []
            if STRATEGIES_DIR.exists():
                names = sorted(p.stem for p in STRATEGIES_DIR.glob("*.json"))
            return self._json({"strategies": names, "dir": str(STRATEGIES_DIR)})

        if len(parts) == 2 and parts[0] == "strategies":
            from ctools.strategy import STRATEGIES_DIR
            name = urllib.parse.unquote(parts[1])
            p = STRATEGIES_DIR / f"{name}.json"
            if not p.exists():
                return self._error(404, f"Strategy not found: {name}")
            try:
                return self._json(json.loads(p.read_text()))
            except json.JSONDecodeError as e:
                return self._error(500, f"Bad strategy JSON: {e}")

        if parts == ["filters"]:
            from ctools.filterlib import FILTERS_DIR
            names = []
            if FILTERS_DIR.exists():
                names = sorted(p.stem for p in FILTERS_DIR.glob("*.json"))
            return self._json({"filters": names, "dir": str(FILTERS_DIR)})

        if len(parts) == 2 and parts[0] == "filters":
            from ctools.filterlib import FILTERS_DIR
            name = urllib.parse.unquote(parts[1])
            p = FILTERS_DIR / f"{name}.json"
            if not p.exists():
                return self._error(404, f"Filter not found: {name}")
            try:
                return self._json(json.loads(p.read_text()))
            except json.JSONDecodeError as e:
                return self._error(500, f"Bad filter JSON: {e}")

        return self._error(404, "Not found")

    # --- POST --------------------------------------------------------------

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:  # noqa: BLE001 - last-resort handler guard
            try:
                self._error(500, f"Internal error: {type(e).__name__}: {e}")
            except Exception:
                pass

    def _do_POST(self):
        parts = self._path()
        if not (parts and parts[0] == "api"):
            return self._error(404, "Not found")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._error(400, "Invalid JSON body")
        if not isinstance(body, dict):
            return self._error(400, "JSON body must be an object")

        if parts[1:] == ["search"]:
            pattern = body.get("pattern", "")
            if not isinstance(pattern, str):
                return self._error(400, "'pattern' must be a string")
            agents = body.get("agents") or ["*"]
            if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
                return self._error(400, "'agents' must be a list of agent names")
            try:
                max_results = int(body.get("max_results", 200))
            except (TypeError, ValueError):
                return self._error(400, "'max_results' must be an integer")
            if max_results < 1:
                return self._error(400, "'max_results' must be >= 1")
            result, err = _do_search(
                pattern,
                agents,
                bool(body.get("ignore_case")),
                max_results,
                body.get("fmt", "default"),
            )
            if err:
                return self._error(400, err)
            return self._json(result)

        if parts[1:] == ["copy"]:
            # Delegate to the ccopy CLI so the full destination syntax works
            # for free (bare agent, agent/session, ssh://[user@]host[:port]/
            # agent). The source is the local agent/session we're viewing.
            source = body.get("source", "")
            dest = body.get("destination", "")
            if not isinstance(source, str) or not isinstance(dest, str):
                return self._error(400, "'source' and 'destination' must be strings")
            dest = dest.strip()
            if not source or "/" not in source:
                return self._error(400, "Source must be agent/session_id")
            if not dest:
                return self._error(400, "Destination is required")
            # Reject lone surrogates / undecodable text before it reaches the
            # subprocess argv (which would otherwise raise UnicodeEncodeError).
            try:
                source.encode("utf-8")
                dest.encode("utf-8")
            except UnicodeEncodeError:
                return self._error(400, "Source/destination contains invalid characters")
            try:
                import subprocess
                proc = subprocess.run(
                    [sys.executable, "-m", "ctools.ccopy", source, dest],
                    capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                return self._error(504, "Copy timed out")
            except Exception as e:  # pragma: no cover - unexpected subprocess failure
                return self._error(500, f"Copy failed: {e}")
            out = proc.stdout or ""
            # ccopy uses a Rich Console on stdout (stderr is empty) for its normal
            # errors, but an unhandled exception (e.g. a missing source session)
            # prints a Python traceback to stderr. Fold both together so the
            # error is always surfaced. Strip ANSI and surface the best line.
            ansi = re.compile(r"\x1b\[[0-9;]*m")
            combined = ansi.sub("", out + ("\n" + (proc.stderr or "")))
            lines = [l.strip() for l in combined.splitlines() if l.strip()]
            if proc.returncode != 0:
                if "Traceback (most recent call last):" in combined:
                    msg = lines[-1] if lines else "Copy failed"
                else:
                    # First line is the actual error message.
                    msg = lines[0] if lines else "Copy failed"
                return self._error(400, msg)
            return self._json(self._parse_copy_result(out, dest))

        if parts[1:] == ["strategies"] or parts[1:] == ["filters"]:
            return self._save_config(parts[1], body)

        return self._error(404, "Not found")

    def _parse_copy_result(self, out: str, dest: str) -> dict:
        """Parse ccopy's stdout into {new_session, destination, messages,
        resume}. ccopy prints Rich output, so strip ANSI first. The success
        line looks like:
            Copied N message(s) from <src> to a new <agent> session: <id>
        followed (optionally) by a resume block:
            Resume it... with:
              <resume command>
        """
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        text = ansi.sub("", out)
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]

        new_id = None
        count = None
        m = re.search(r"Copied\s+(\d+)\s+message\(s\)\s+from\s+.+?to a new\s+(\S+)\s+session(?::\s*(\S+))?",
                      text)
        if m:
            count = int(m.group(1))
            new_id = m.group(3)  # may be None if the id is on the same token
        # Fallback: the new session id is the last whitespace token on the
        # "Copied ... session" line.
        if new_id is None:
            for l in lines:
                if l.startswith("Copied") and "session" in l:
                    new_id = l.split()[-1]
                    break
        # Message count fallback.
        if count is None:
            cm = re.search(r"Copied\s+(\d+)\s+message", text)
            if cm:
                count = int(cm.group(1))

        # Resume command: the indented line after a "Resume" line.
        resume = None
        for i, l in enumerate(lines):
            if "Resume" in l and i + 1 < len(lines):
                resume = lines[i + 1].strip()
                break

        return {
            "new_session": new_id,
            "destination": dest,
            "messages": count,
            "resume": resume,
        }

    def _save_config(self, kind: str, body: dict):
        """Create/update a strategy or filter config by name."""
        from ctools.strategy import STRATEGIES_DIR
        from ctools.filterlib import FILTERS_DIR
        name = (body.get("name") or "").strip()
        cfg = body.get("config")
        if not name or not isinstance(cfg, dict):
            return self._error(400, "Need a 'name' and a 'config' object")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return self._error(400, "Name may only contain letters, digits, _ . -")
        d = STRATEGIES_DIR if kind == "strategies" else FILTERS_DIR
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.json"
        p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        return self._json({"name": name, "kind": kind, "path": str(p)})

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ctools</title>
<style>
:root {
  --bg: #0f1117; --panel: #161a23; --panel2: #1d2230;
  --border: #262d3d; --text: #dbe2ef; --dim: #8b95ab;
  --accent: #5aa9ff; --green: #57c78a; --red: #e5695e;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; align-items: baseline; gap: 14px; padding: 14px 22px;
  border-bottom: 1px solid var(--border); background: var(--panel);
  position: sticky; top: 0; z-index: 5; }
header h1 { font-size: 17px; margin: 0; font-weight: 700; letter-spacing: .3px; cursor: pointer; }
header h1:hover { color: var(--accent); }
header h1 span { color: var(--accent); }
header .ver { color: var(--dim); font-size: 12px; font-family: var(--mono); cursor: pointer; }
header .ver:hover { color: var(--accent); text-decoration: underline; }
nav { display: flex; gap: 4px; margin-left: auto; }
nav button { background: none; border: 1px solid transparent; color: var(--dim);
  padding: 6px 12px; border-radius: 8px; cursor: pointer; font-size: 13px; }
nav button:hover { color: var(--text); background: var(--panel2); }
nav button.on { color: var(--text); background: var(--panel2);
  border-color: var(--border); }
main { padding: 20px 22px 60px; max-width: 1100px; margin: 0 auto; }
.view { display: none; } .view.on { display: block; }
h2 { font-size: 15px; margin: 22px 0 10px; color: var(--dim);
  text-transform: uppercase; letter-spacing: .8px; font-weight: 600; }
h2:first-child { margin-top: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
  gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px; }
.card.click { cursor: pointer; }
.card.click:hover { border-color: var(--accent); transform: translateY(-1px); }
.card .name { font-family: var(--mono); font-weight: 700; font-size: 14px; }
.card .desc { color: var(--dim); font-size: 12px; margin: 2px 0 8px;
  min-height: 2.4em; }
.card .meta { display: flex; gap: 10px; font-size: 12px; color: var(--dim); }
.card .meta b { color: var(--text); font-weight: 600; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 99px;
  font-size: 11px; font-weight: 600; }
.badge.ok { background: rgba(87,199,138,.15); color: var(--green); }
.badge.no { background: rgba(229,105,94,.15); color: var(--red); }
.linkbtn { background: none; border: none; color: var(--accent); cursor: pointer;
  font-size: 12px; padding: 0; text-decoration: underline; }
input, select { background: var(--panel2); border: 1px solid var(--border);
  color: var(--text); border-radius: 8px; padding: 7px 10px; font-size: 13px;
  font-family: var(--mono); }
textarea { background: var(--panel2); border: 1px solid var(--border);
  color: var(--text); border-radius: 8px; padding: 9px 10px; font-size: 13px;
  font-family: var(--mono); line-height: 1.5; }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
.field { display: flex; flex-direction: column; gap: 5px; margin-bottom: 12px; }
.field > label { font-size: 12px; font-weight: 600; color: var(--text); }
.field > label span { color: var(--dim); font-weight: 400; }
.field > .hint { font-size: 11.5px; color: var(--dim); line-height: 1.45; }
.field input, .field select, .field textarea { width: 100%; font-size: 14px;
  padding: 9px 12px; background: var(--bg); }
.field textarea { min-height: 90px; resize: vertical; }
.fgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
@media (max-width: 640px) { .fgrid { grid-template-columns: 1fr; } }
textarea#st-prompt, textarea#fl-instr, textarea#fl-cmd { min-height: 150px; }
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.toolbar input[type=text], .toolbar input[type=search] { flex: 1; min-width: 200px; }
button.primary { background: var(--accent); border: none; color: #08111f;
  font-weight: 600; border-radius: 8px; padding: 7px 14px; cursor: pointer;
  font-size: 13px; }
button.primary:disabled { opacity: .5; cursor: default; }
.modelrow { display: flex; gap: 6px; }
.modelrow input { flex: 1; min-width: 0; }
.modelrow button { flex: none; background: var(--panel2); border: 1px solid var(--border);
  color: var(--dim); border-radius: 8px; padding: 0 12px; font-size: 12px;
  cursor: pointer; white-space: nowrap; }
.modelrow button:hover { border-color: var(--accent); color: var(--accent); }
.modelrow button:disabled { opacity: .5; cursor: default; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; color: var(--dim); font-size: 11px; text-transform: uppercase;
  letter-spacing: .6px; padding: 6px 10px; border-bottom: 1px solid var(--border); }
th.sorth { cursor: pointer; user-select: none; white-space: nowrap; }
th.sorth:hover { color: var(--accent); }
td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 13px;
  vertical-align: top; }
tr.click { cursor: pointer; } tr.click:hover td { background: var(--panel2); }
td.mono, .mono { font-family: var(--mono); font-size: 12px; }
td .sub { color: var(--dim); font-size: 11px; }
.msg { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
.msg .role { font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .7px; margin-bottom: 5px; }
.msg .role.user { color: var(--accent); }
.msg .role.assistant { color: var(--green); }
.msg .role.system { color: #b48ce0; }
.msg .role.tool { color: #e0a458; }
.msg pre { margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: var(--mono); font-size: 12.5px; line-height: 1.55; }
.msg.collapsed pre { display: none; }
.msg .expand { color: var(--accent); font-size: 11px; cursor: pointer;
  font-family: var(--mono); }
.match { padding: 8px 12px; border-bottom: 1px solid var(--border);
  font-family: var(--mono); font-size: 12px; }
.match .ref { color: var(--accent); cursor: pointer; }
.match .ref:hover { text-decoration: underline; }
.match .ln { color: var(--dim); margin-right: 8px; }
.chip { display: inline-block; background: var(--panel2); border: 1px solid var(--border);
  border-radius: 99px; padding: 2px 10px; font-size: 12px; margin: 2px 4px 2px 0;
  cursor: pointer; }
.chip.on { background: var(--accent); color: #08111f; border-color: var(--accent);
  font-weight: 600; }
.concept { background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; }
.concept .type { font-size: 11px; font-weight: 700; color: var(--green);
  text-transform: uppercase; letter-spacing: .6px; }
.concept pre { margin: 4px 0 0; white-space: pre-wrap; font-family: var(--mono);
  font-size: 12px; }
.notice { background: rgba(87,199,138,.1); border: 1px solid rgba(87,199,138,.4);
  color: var(--green); padding: 10px 14px; border-radius: 8px; margin: 12px 0;
  font-size: 13px; }
.err { background: rgba(229,105,94,.1); border: 1px solid rgba(229,105,94,.4);
  color: var(--red); padding: 10px 14px; border-radius: 8px; margin: 12px 0; }
.dim { color: var(--dim); } .small { font-size: 12px; }
code { font-family: var(--mono); background: var(--panel2); padding: 1px 6px;
  border-radius: 5px; font-size: 12px; }
.explain { border-left: 3px solid var(--accent); background: var(--panel);
  border-radius: 0 8px 8px 0; padding: 10px 14px; margin: 10px 0; font-size: 13px;
  color: var(--text); }
.explain .dim { display: block; margin-top: 6px; }
.explain b { color: var(--accent); }
.examplebox { margin: 12px 0; border: 1px solid var(--border); border-radius: 8px; }
.examplebox summary { cursor: pointer; padding: 8px 12px; font-size: 12px;
  color: var(--dim); font-weight: 600; }
.examplebox pre { margin: 0; padding: 12px 14px; font-family: var(--mono);
  font-size: 11.5px; line-height: 1.5; overflow-x: auto; border-top: 1px solid var(--border);
  white-space: pre; color: var(--text); }
.pager { margin-top: 10px; color: var(--dim); font-size: 12px; }
.copybanner { position: sticky; top: 60px; z-index: 9; margin-bottom: 14px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 16px; box-shadow: 0 6px 20px rgba(0,0,0,.45); }
.copybanner .cmdrow { display: flex; gap: 8px; align-items: stretch; margin-top: 8px;
  flex-wrap: wrap; }
.copybanner .cmdrow code { flex: 1; white-space: nowrap; overflow-x: auto;
  padding: 7px 10px; font-size: 12.5px; }
.copybanner .rowbtns { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap;
  align-items: center; }
.copybanner .muted { color: var(--dim); font-size: 12px; }
.copied-flash { color: var(--green); font-size: 12px; font-weight: 600; }
/* Copy destination: a horizontal inline card (label + input + button). */
.copycard { display: inline-flex; align-items: center; gap: 8px;
  background: var(--panel2); border: 1px solid var(--border); border-radius: 10px;
  padding: 5px 6px 5px 12px; flex-wrap: wrap; max-width: 100%; }
.copycard-label { color: var(--dim); font-size: 11px; text-transform: uppercase;
  letter-spacing: .6px; font-weight: 600; }
.copycard input { flex: 1; min-width: 220px; background: var(--panel); }
</style>
</head>
<body>
<header>
  <h1>ctools<span>.</span></h1>
  <span class="ver" id="ver"></span>
  <nav>
    <button data-view="overview">Overview</button>
    <button data-view="search">Search</button>
    <button data-view="sessions">Sessions</button>
    <button data-view="converse">Conversation</button>
    <button data-view="configs">Strategies &amp; Filters</button>
  </nav>
</header>
<main>
  <section id="view-overview" class="view on">
    <h2>Installed agents</h2>
    <p class="dim small">Sorted by conversation volume. Click a card to browse its sessions.</p>
    <div id="agent-grid" class="grid" style="margin-bottom:26px"></div>
    <h2>Not found on this machine</h2>
    <p class="dim small">Supported by ctools, but no storage found yet. They light up here as soon as you use them.</p>
    <div id="agent-grid-missing" class="grid"></div>
    <p class="dim small" style="margin-top:18px">
      ctools moves memory between context windows. The source is never touched.
      Everything here is read-only except <b>Copy</b>, which creates a new session
      in the destination agent.
    </p>
  </section>

  <section id="view-search" class="view">
    <h2>Search every session</h2>
    <div class="toolbar">
      <input type="search" id="q" placeholder="regex, e.g. snake game|TODO|ssl" autofocus>
      <label class="small dim" style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="qi" style="width:auto"> -i
      </label>
      <button class="primary" id="qbtn">Search</button>
    </div>
    <div id="q-agents"></div>
    <div id="q-result"></div>
  </section>

  <section id="view-sessions" class="view">
    <h2>Sessions</h2>
    <div class="toolbar">
      <select id="s-agent"></select>
      <select id="s-sort">
        <option value="time">newest first</option>
        <option value="size">largest first</option>
        <option value="tokens">most tokens first</option>
      </select>
      <input type="search" id="s-filter" placeholder="filter: id, name or model…" style="flex:1;min-width:180px">
      <button class="primary" id="s-btn">List</button>
    </div>
    <div id="s-result"></div>
  </section>

  <section id="view-configs" class="view">
    <div style="display:flex;gap:24px;flex-wrap:wrap">
      <div style="flex:1;min-width:320px">
        <h2>Extraction strategies</h2>
        <p class="dim small">Point an LLM at a conversation and have it chunk
          the concepts (the <b>LLM</b> path). Stored in
          <code>~/.config/ctools/strategies/</code>.</p>
        <div id="strat-list" class="grid" style="margin-bottom:12px"></div>
        <div class="explain"><b>What is a strategy?</b>
          A strategy tells ctools how to <b>chunk a conversation into concepts</b>
          using an LLM. During extraction the model is asked to read the
          conversation and return the key points — goals, constraints,
          decisions, open questions. <span class="dim">The alternative is the built-in
          regex path ("none" in the Concepts panel), which uses pattern
          matching and no model. The saved file is plain JSON — the form below
          just writes it for you.</span>
        </div>
        <details class="examplebox"><summary>Example strategy file (what gets saved)</summary>
<pre>{
  "host": "http://localhost:11434",   // any OpenAI-compatible server (Ollama default)
  "model": "qwen2.5:3b",              // model name on that server
  "api_key": null,                    // leave null for Ollama; set for OpenAI etc.
  "prompt": "Extract the key concepts..."  // instructions to the LLM
}</pre>
        </details>
        <h2 style="font-size:12px">New strategy</h2>
        <div id="strat-form" class="card" style="margin-bottom:8px"></div>
        <div style="display:flex;gap:8px">
          <button class="primary" onclick="saveStrategy()">Create strategy</button>
        </div>
      </div>
      <div style="flex:1;min-width:320px">
        <h2>Filters</h2>
        <p class="dim small">Select which extracted concepts pass through. Three
          backends: <b>decision model</b> (local classifier via Ollama
          systemone/chat), <b>built-in</b> (type &amp; substring match, no
          server), and <b>JSON-RPC</b> (your own subprocess). Stored in
          <code>~/.config/ctools/filters/</code>.</p>
        <div id="filter-list" class="grid" style="margin-bottom:12px"></div>
        <div class="explain"><b>What is a filter?</b>
          After concepts are extracted (by a strategy or the regex path),
          a filter decides <b>which ones pass through</b>. Three backends:
          <span class="dim">
          <b>decision model</b> — a local classifier (e.g. a systemone model on
          Ollama) answers yes/no or picks a label per concept;
          <b>built-in</b> — pure code: keep by concept type and/or required
          substring, no server needed;
          <b>JSON-RPC</b> — your own subprocess implements
          <code>notify</code>/<code>request</code> methods over stdin/stdout. Like
          strategies, filters are plain JSON files.</span>
        </div>
        <details class="examplebox"><summary>Example filter files</summary>
<pre>// decision model — ask a local classifier per concept
{
  "type": "systemone",
  "host": "http://localhost:11434",
  "model": "tev1",
  "endpoint": "systemone",     // or "chat" for OpenAI-compatible
  "instructions": "Is this about security?",
  "mode": "noul",              // or "choice" for routing
  "threshold": 0.6
}

// built-in — no server: keep by type and/or substring
{
  "types": ["constraint", "goal"],
  "exclude_types": [],
  "prompt": "ssl"              // require this substring
}

// JSON-RPC — your own subprocess over stdin/stdout
{
  "type": "jsonrpc",
  "command": ["python", "filter.py"],
  "method": "classify",
  "timeout": 30
}</pre>
        </details>
        <h2 style="font-size:12px">New filter</h2>
        <div id="filter-form" class="card" style="margin-bottom:8px"></div>
        <div style="display:flex;gap:8px">
          <button class="primary" onclick="saveFilter()">Create filter</button>
        </div>
      </div>
    </div>
  </section>

  <section id="view-converse" class="view">
    <h2 id="c-title">Conversation</h2>
    <div id="c-banner" class="copybanner" style="display:none"></div>
    <div id="c-meta" class="toolbar" style="align-items:center"></div>
    <div id="c-body"></div>
    <pre id="c-export" style="display:none;background:var(--panel);border:1px solid
      var(--border);border-radius:10px;padding:14px;font-size:12px;max-height:60vh;overflow:auto"></pre>
  </section>
</main>

<script>
const $ = s => document.querySelector(s);
const esc = s => (s ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let AGENTS = [];
let STRATEGIES = [];
let FILTERS = [];
let CONV = null;   // {agent, session_id}
let LAST_SESSIONS = null;

function switchView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("on"));
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("on", b.dataset.view === name));
  $("#view-" + name).classList.add("on");
}
// --- hash routing: #, #search, #sessions/<agent>[?sort=x], #session/<agent>/<sid>, #configs ---
function parseHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (!h) return {view: "overview"};
  const parts = h.split("/");
  if (parts[0] === "search") return {view: "search"};
  if (parts[0] === "configs") return {view: "configs"};
  if (parts[0] === "sessions" && parts[1]) {
    const seg = parts[1].split("?");
    let sort = "time";
    (seg[1] ? seg[1].split("&") : []).forEach(kv => {
      const [k, v] = kv.split("=");
      if (k === "sort" && v) sort = v;
    });
    return {view: "sessions", agent: seg[0], sort};
  }
  if (parts[0] === "session" && parts[1] && parts[2]) {
    return {view: "converse", agent: parts[1], sid: parts[2]};
  }
  return {view: "overview"};
}

function navigate(hash) {
  const cur = location.hash.replace(/^#/, "");
  if (cur === hash) return;
  history.pushState(null, "", hash ? "#" + hash : location.pathname + location.search);
  applyRoute();
}

function applyRoute() {
  const r = parseHash();
  switchView(r.view);
  if (r.view === "configs" && !window._configsLoaded) {
    window._configsLoaded = true;
    loadConfigs().then(() => { stratFormFields(); filterFormFields(); });
  }
  if (r.view === "sessions") {
    const known = [...$("#s-agent").options].some(o => o.value === r.agent);
    $("#s-agent").value = known ? r.agent : $("#s-agent").value;
    if (r.sort) $("#s-sort").value = r.sort;
    const key = $("#s-agent").value + "|" + $("#s-sort").value;
    if (key !== window._shownSessKey) { window._shownSessKey = key; listSessions(); }
  }
  if (r.view === "converse" &&
      (!CONV || CONV.agent !== r.agent || CONV.session_id !== r.sid)) {
    openConverse(r.agent, r.sid);
  }
}
window.addEventListener("hashchange", applyRoute);
window.addEventListener("popstate", applyRoute);

document.querySelectorAll("nav button").forEach(b =>
  b.addEventListener("click", () => {
    const v = b.dataset.view;
    if (v === "overview") navigate("");
    else if (v === "search") navigate("search");
    else if (v === "configs") navigate("configs");
    else if (v === "sessions") {
      const a = $("#s-agent").value || ((AGENTS.find(x => x.installed) || {}).name);
      if (a) navigate(sessionsHash());
    }
    else if (v === "converse" && CONV) {
      navigate(`session/${CONV.agent}/${CONV.session_id}`);
    }
  }));

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
function humanBytes(n) {
  if (n == null) return "-";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n/1024).toFixed(1) + " KB";
  return (n/1048576).toFixed(1) + " MB";
}
function humanTok(n) {
  if (n == null) return "-";
  if (n < 1000) return String(n);
  if (n < 1000000) return (n/1000).toFixed(1) + "k";
  return (n/1000000).toFixed(2) + "M";
}

async function copyToClipboard(text, el) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // fallback for non-secure contexts (http on LAN etc.)
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (_) {}
    ta.remove();
  }
  if (el) {
    const orig = el.textContent;
    el.textContent = "copied ✓";
    el.classList.add("copied-flash");
    setTimeout(() => { el.textContent = orig; el.classList.remove("copied-flash"); }, 1500);
  }
}

async function loadOverview() {
  const data = await api("/api/agents");
  $("#ver").textContent = "v" + data.version;
  AGENTS = data.agents;
  const grid = $("#agent-grid");
  const gridMissing = $("#agent-grid-missing");
  grid.innerHTML = "";
  gridMissing.innerHTML = "";
  // installed first, biggest conversation volume on top
  const installed = AGENTS.filter(a => a.installed)
    .sort((x, y) => ((y.total_msgs || 0) - (x.total_msgs || 0))
      || ((y.session_count || 0) - (x.session_count || 0)));
  for (const a of AGENTS) {
    if (a.installed) continue;
    const card = document.createElement("div");
    card.className = "card dim";
    card.style.opacity = "0.6";
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div class="name">${esc(a.name)}</div>
        <span class="badge no">not found</span>
      </div>
      <div class="desc">${esc(a.description)}</div>
      <div class="meta"><span class="mono">${esc(a.storage)}</span></div>`;
    gridMissing.appendChild(card);
  }
  if (!gridMissing.children.length) {
    gridMissing.innerHTML = `<div class="card dim">Every supported agent is installed here. 🎉</div>`;
  }
  for (const a of installed) {
    const card = document.createElement("div");
    card.className = "card click";
    card.onclick = () => openSessions(a.name);
    const msgs = a.total_msgs || 0;
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div class="name">${esc(a.name)}</div>
        <span class="badge ok">installed</span>
      </div>
      <div class="desc">${esc(a.description)}</div>
      <div class="meta">
        <span>${msgs.toLocaleString()} msgs</span>
        <span>${a.session_count ?? 0} sessions</span>
        <span>${humanTok(a.total_tokens)} tok</span>
      </div>
      <div style="margin-top:8px">
        <button class="linkbtn">browse sessions →</button>
      </div>`;
    grid.appendChild(card);
  }
  if (!installed.length) {
    grid.innerHTML = `<div class="card dim">No agents found yet. Run one of the supported tools and refresh.</div>`;
  }
  // search agent chips
  $("#q-agents").innerHTML =
    `<span class="chip on" data-a="*">all</span>` +
    AGENTS.filter(a => a.installed).map(a =>
      `<span class="chip" data-a="${esc(a.name)}">${esc(a.name)}</span>`).join("");
  document.querySelectorAll("#q-agents .chip").forEach(c =>
    c.onclick = () => {
      document.querySelectorAll("#q-agents .chip")
        .forEach(x => x.classList.remove("on"));
      c.classList.add("on");
      // clicking an agent chip searches immediately — no extra button click
      if ($("#q").value.trim()) doSearch();
    });
  // sessions select
  $("#s-agent").innerHTML = `<option value="all">all agents</option>` +
    AGENTS.filter(a => a.installed)
    .map(a => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("");
}

function sessionsHash() {
  return `sessions/${encodeURIComponent($("#s-agent").value)}?sort=${$("#s-sort").value}`;
}

function openSessions(name) {
  if ($("#s-agent").value !== name &&
      [...$("#s-agent").options].some(o => o.value === name))
    $("#s-agent").value = name;
  const a = $("#s-agent").value || name;
  const cur = location.hash.replace(/^#/, "");
  if (cur === sessionsHash()) { switchView("sessions"); listSessions(); }
  else navigate(sessionsHash());
}

let sessSeq = 0;
async function listSessions() {
  const name = $("#s-agent").value;
  const sort = $("#s-sort").value;
  const seq = ++sessSeq;
  window._shownSessKey = name + "|" + sort;
  $("#s-result").innerHTML = `<p class="dim">Loading…</p>`;
  try {
    const d = await api(`/api/agents/${encodeURIComponent(name)}/sessions?sort=${sort}`);
    if (seq !== sessSeq) return;  // a newer request superseded this one
    LAST_SESSIONS = {agent: name, sessions: d.sessions, total: d.total};
    window._sessSort = {col: "mtime", dir: -1};  // server returns newest-first by default
    renderSessions();
  } catch (e) {
    if (seq !== sessSeq) return;
    $("#s-result").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

function renderSessions() {
  if (!LAST_SESSIONS) return;
  const {agent, sessions, total} = LAST_SESSIONS;
  const f = $("#s-filter").value.trim().toLowerCase();
  const isAll = agent === "all";
  const shown = f ? sessions.filter(s =>
    (s.id || "").toLowerCase().includes(f) ||
    (s.name || "").toLowerCase().includes(f) ||
    (s.path || "").toLowerCase().includes(f) ||
    (s.agent || "").toLowerCase().includes(f)) : sessions;
  const rows = shown.map(s => {
    const a = s.agent || agent;
    return `
      <tr class="click" onclick="openConverse('${esc(a)}','${esc(s.id)}')">
        <td style="max-width:280px"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.name || s.id)}</div>
        <div class="sub mono">${esc(s.id)}</div></td>
        ${isAll ? `<td class="mono">${esc(s.agent || "")}</td>` : ""}
        <td>${esc(s.mtime ? s.mtime.slice(0,16).replace("T"," ") : "-")}</td>
        <td class="mono" style="max-width:340px"><span title="${esc(s.path || "")}"
          onclick="event.stopPropagation();copyToClipboard('${esc(s.path || "")}',this)"
          >${esc(s.path || "-")}</span></td>
        <td class="mono">${s.message_count ?? "-"}</td>
      </tr>`;
  }).join("");
  const sort = window._sessSort || {col: "mtime", dir: -1};  // default: newest first
  const arrow = (col) => col === sort.col
    ? (sort.dir === -1 ? " ↓" : " ↑") : "";
  const th = (col, label) =>
    `<th class="sorth" onclick="sortSessions('${col}')" title="Click to sort">${label}${arrow(col)}</th>`;
  const agentTh = isAll ? th("agent", "Agent") : "";
  const colspan = isAll ? 5 : 4;
  $("#s-result").innerHTML = `
      <table><thead><tr>${th("name","Session")}${agentTh}${th("mtime","Modified")}${th("path","Path")}${th("message_count","Msgs")}</tr></thead>
      <tbody>${rows || `<tr><td colspan="${colspan}" class="dim">No sessions match “${esc(f)}”.</td></tr>`}</tbody></table>
      <div class="pager">${total} session(s)${total > sessions.length
        ? " — first " + sessions.length + " loaded" : ""}${f
        ? ` · ${shown.length} match(es)` : ""}</div>`;
}

// Click-to-sort on the Sessions table columns (client-side, works for “all” too).
function sortSessions(col) {
  if (!LAST_SESSIONS) return;
  const cur = window._sessSort || {col: "mtime", dir: -1};
  const dir = (cur.col === col) ? -cur.dir : -1;  // toggle if same col, else newest/largest first
  window._sessSort = {col, dir};
  const s = LAST_SESSIONS.sessions;
  const key = {
    name:  (x) => (x.name || x.id || "").toLowerCase(),
    agent: (x) => (x.agent || "").toLowerCase(),
    mtime: (x) => x.mtime || x.ctime || "",
    path:  (x) => (x.path || "").toLowerCase(),
    message_count: (x) => (x.message_count ?? 0),
  }[col];
  s.sort((a, b) => {
    const av = key(a), bv = key(b);
    if (av === bv) return 0;
    return (av > bv ? 1 : -1) * dir;
  });
  renderSessions();
}

async function doSearch() {
  const pattern = $("#q").value;
  if (!pattern) return;
  const sel = document.querySelector("#q-agents .chip.on");
  const agents = sel ? [sel.dataset.a] : ["*"];
  $("#q-result").innerHTML = `<p class="dim">Searching…</p>`;
  try {
    const d = await api("/api/search", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({pattern, agents, ignore_case: $("#qi").checked,
                            max_results: 300}),
    });
    if (!d.matches.length) {
      $("#q-result").innerHTML = `<p class="dim">No matches for “${esc(pattern)}”.</p>`;
      return;
    }
    const per = Object.entries(d.per_agent || {})
      .map(([a,n]) => `${esc(a)} <b>${n}</b>`).join(" · ");
    $("#q-result").innerHTML = `
      <p class="small dim" style="margin-bottom:8px">${d.total} match(es) — ${per}</p>
      <div>${d.matches.map(m => `
        <div class="match">
          <span class="ref" onclick="openConverse('${esc(m.agent)}','${esc(m.session_id)}')">
            ${esc(m.agent)}/${esc(m.session_id)}</span>
          <span class="ln">${m.line_num}</span>${esc(m.line)}
        </div>`).join("")}</div>`;
  } catch (e) {
    $("#q-result").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

async function openConverse(agent, sid) {
  if (CONV && CONV.agent === agent && CONV.session_id === sid) { switchView("converse"); return; }
  const cur = location.hash.replace(/^#/, "");
  if (cur === `session/${agent}/${sid}`) switchView("converse");
  else navigate(`session/${agent}/${sid}`);
  $("#c-title").textContent = `${agent} / ${sid}`;
  $("#c-body").innerHTML = `<p class="dim">Loading…</p>`;
  $("#c-meta").innerHTML = "";
  dismissBanner();
  CONV = {agent, session_id: sid};
  try {
    const d = await api(`/api/agents/${encodeURIComponent(agent)}/sessions/${encodeURIComponent(sid)}`);
    let meta = `<span class="small dim">${d.messages.length} messages
      · ${d.tokens ? (humanTok(d.tokens.total) + (d.estimated ? " tok (est.)" : " tok")) : ""}</span>`;
    meta += ` <span class="copycard" id="copycard">
      <span class="copycard-label">Copy to</span>
      <input id="c-dest" type="text" autocomplete="off" spellcheck="false"
        placeholder="agent, agent/session, or ssh://user@host/agent"
        value="" />
      <button class="primary" onclick="doCopy()">Copy</button>
    </span>`;
    meta += ` <button class="primary" style="background:var(--green)"
      onclick="showConcepts()">Concepts</button>`;
    meta += ` <button class="primary" style="background:var(--panel2)"
      onclick="showExport()">Export</button>`;
    if (d.resume) meta += ` <code style="margin-left:8px">${esc(d.resume)}</code>`;
    $("#c-meta").innerHTML = meta;
    $("#c-dest").addEventListener("keydown", e => {
      if (e.key === "Enter") doCopy();
    });
    $("#c-body").innerHTML = d.messages.map((m, i) => msgHtml(m, i)).join("");
  } catch (e) {
    $("#c-body").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

function msgHtml(m, i) {
  const full = m.content;
  const long = full.length > 900;
  const shown = long ? full.slice(0, 900) + " …" : full;
  return `<div class="msg">
    <div style="display:flex;justify-content:space-between;gap:12px">
      <span class="role ${esc(m.role)}">${esc(m.role)}</span>
      ${long ? `<span class="expand">show more ▾</span>` : ""}
    </div>
    <pre data-full="${esc(full)}">${esc(shown)}</pre></div>`;
}

// (superseded by the extraction flow below — showConcepts now lives with it)

// Copy: read the destination from the horizontal input card and POST it.
// Any ccopy destination syntax is accepted (bare agent, agent/session, or
// ssh://[user@]host[:port]/agent) because the backend shells out to ccopy.
function doCopy() {
  if (!CONV) return;
  const input = $("#c-dest");
  const dest = input ? input.value.trim() : "";
  if (!dest) {
    if (input) input.focus();
    return;
  }
  doCopyTo(dest);
}

async function doCopyTo(dest) {
  if (!CONV) return;
  const banner = $("#c-banner");
  banner.style.display = "block";
  banner.innerHTML = `
    <div class="notice" style="margin:0">Copying
      <span class="mono">${esc(CONV.agent)}/${esc(CONV.session_id)}</span> →
      <b>${esc(dest)}</b>… source is not modified.</div>`;
  try {
    const d = await api("/api/copy", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source: `${CONV.agent}/${CONV.session_id}`,
                            destination: dest}),
    });
    const resume = d.resume || "";
    const isRemote = /^ssh:\/\//.test(d.destination);
    const ccopyCmd = `ccopy ${CONV.agent}/${CONV.session_id} ${d.destination}`;
    banner.innerHTML = `
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:start;flex-wrap:wrap">
        <div>
          <div class="notice" style="margin:0">✓ Copied <b>${d.messages}</b> message(s) to a new
            <b>${esc(d.destination)}</b> session${isRemote ? " (on remote host)" : ""}:
            <span class="mono">${esc(d.new_session)}</span></div>
          ${resume ? `<div class="cmdrow">
            <code>${esc(resume)}</code>
            <button class="primary" data-cp="${esc(resume)}">copy command</button>
          </div>` : ""}
          <div class="cmdrow">
            <code>${esc(ccopyCmd)}</code>
            <button class="primary" data-cp="${esc(ccopyCmd)}">copy command</button>
          </div>
          <div class="rowbtns">
            ${!isRemote ? `<button class="primary" style="background:var(--panel2)"
              onclick="openConverse('${esc(d.destination)}','${esc(d.new_session)}')">view new session →</button>` : ""}
            <button class="linkbtn" onclick="dismissBanner()">dismiss</button>
          </div>
        </div>
      </div>`;
    banner.querySelectorAll("[data-cp]").forEach(b =>
      b.onclick = () => copyToClipboard(b.dataset.cp, b));
    banner.scrollIntoView({behavior: "smooth", block: "nearest"});
  } catch (e) {
    banner.innerHTML = `<div class="err" style="margin:0">${esc(e.message)}</div>
      <div class="rowbtns"><button class="linkbtn" onclick="dismissBanner()">dismiss</button></div>`;
  }
}

function dismissBanner() {
  $("#c-banner").style.display = "none";
  $("#c-banner").innerHTML = "";
}

function sessionsChange() {
  history.replaceState(null, "", "#" + sessionsHash());
  listSessions();
}

// events
$("#qbtn").addEventListener("click", doSearch);
// logo → overview; version → GitHub repo
$("header h1").addEventListener("click", () => navigate(""));
$("#ver").addEventListener("click", () =>
  window.open("https://github.com/day50-dev/ctools", "_blank", "noopener"));
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });
$("#s-btn").addEventListener("click", listSessions);
$("#s-agent").addEventListener("change", sessionsChange);
$("#s-sort").addEventListener("change", sessionsChange);
$("#s-filter").addEventListener("input", renderSessions);

// expand/collapse long messages
document.addEventListener("click", e => {
  const el = e.target.closest(".expand");
  if (!el) return;
  const pre = el.closest(".msg").querySelector("pre");
  const full = pre.dataset.full || pre.textContent;
  if (el.textContent.startsWith("show")) {
    pre.textContent = full;
    el.textContent = "collapse ▴";
  } else {
    pre.textContent = full.slice(0, 900) + " …";
    el.textContent = "show more ▾";
  }
});

// --- Strategies & Filters (configs view) --------------------------------
async function loadConfigs() {
  const [s, f] = await Promise.all([api("/api/strategies"), api("/api/filters")]);
  STRATEGIES = s.strategies || [];
  FILTERS = f.filters || [];
  renderStrategies();
  renderFilters();
}

function stratCard(name) {
  return `<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div class="name">${esc(name)}</div>
      <button class="linkbtn" onclick="viewConfig('strategies','${esc(name)}')">view</button>
    </div>
    <div class="desc">LLM extraction strategy</div>
  </div>`;
}

function filterKind(c) {
  if (!c) return "?";
  if (c.type === "jsonrpc" || (c.command && !c.model)) return "jsonrpc";
  if (c.type === "systemone" || (c.model && c.instructions)) return "decision";
  if (c.types || c.prompt || c.exclude_types) return "built-in";
  return "?";
}

function filterCard(name, c) {
  const kind = filterKind(c);
  const detail = kind === "decision"
    ? `${esc(c.model || "")} · ${esc(c.endpoint || "systemone")} · ${esc(c.mode || "noul")}`
    : kind === "built-in"
      ? esc((c.types || []).join(", ") || "any type")
      : esc(Array.isArray(c.command) ? c.command.join(" ") : (c.command || ""));
  return `<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div class="name">${esc(name)}</div>
      <button class="linkbtn" onclick="viewConfig('filters','${esc(name)}')">view</button>
    </div>
    <div class="desc">${esc(kind)} filter</div>
    <div class="meta"><span class="mono">${detail}</span></div>
  </div>`;
}

function renderStrategies() {
  $("#strat-list").innerHTML = STRATEGIES.length
    ? STRATEGIES.map(n => stratCard(n)).join("")
    : `<div class="card dim">No strategies yet. Create one below.</div>`;
}

function renderFilters() {
  $("#filter-list").innerHTML = FILTERS.length
    ? FILTERS.map(n => filterCard(n)).join("")
    : `<div class="card dim">No filters yet. Create one below.</div>`;
}

async function viewConfig(kind, name) {
  const c = await api(`/api/${kind}/${encodeURIComponent(name)}`).catch(() => ({}));
  alert(`${kind === "strategies" ? "Strategy" : "Filter"}: ${name}\n\n`
        + JSON.stringify(c, null, 2));
}

function stratFormFields() {
  $("#strat-form").innerHTML = `
    <div class="field"><label for="st-name">Name</label>
      <input id="st-name" placeholder="e.g. gemma4">
      <div class="hint">File name without extension — becomes
        <code>~/.config/ctools/strategies/&lt;name&gt;.json</code> and the option in the
        Concepts dropdown.</div></div>
    <div class="fgrid">
      <div class="field"><label for="st-host">LLM server <span>host</span></label>
        <input id="st-host" value="http://localhost:11434">
        <div class="hint">Any OpenAI-compatible chat server. Ollama's default is
          <code>http://localhost:11434</code>.</div></div>
      <div class="field"><label for="st-model">Model</label>
        <div class="modelrow">
          <input id="st-model" list="st-models" placeholder="e.g. qwen2.5:3b">
          <button type="button" id="st-fetch" onclick="fetchModels('st-model','st-host','st-models','st-fetch')">fetch models</button>
        </div>
        <datalist id="st-models"></datalist>
        <div class="hint">Must exist on that server — hit “fetch models” to list what
          the server has (Ollama <code>/api/tags</code> or OpenAI <code>/v1/models</code>),
          or check with <code>ollama list</code>.</div></div>
    </div>
    <div class="field"><label for="st-key">API key <span>optional</span></label>
      <input id="st-key" type="password" placeholder="leave empty for Ollama">
      <div class="hint">Only for hosted / OpenAI-style servers; Ollama needs none.
        Stored in the JSON file on disk.</div></div>
    <div class="field"><label for="st-prompt">Extraction prompt</label>
      <textarea id="st-prompt" rows="7" placeholder="Instructions for the LLM…"></textarea>
      <div class="hint">Tells the model what to extract and how to format it. The
        default asks for concept types (goal, constraint, decision, question,
        action, …) with a short text each — keep that shape so filters can
        match on types.</div></div>
    <button class="linkbtn" onclick="fillDefaultPrompt()">use default prompt</button>`;
  fillDefaultPrompt();
}
function fillDefaultPrompt() {
  const el = $("#st-prompt");
  if (el && !el.value) el.value =
    'Extract the key concepts from this conversation. For each concept, output a '
    + 'JSON object with fields: type (constraint|goal|preference|observation|reference), '
    + 'description (<20 words), short (<250 chars), medium (<1000 chars), long (full text). '
    + 'Output a JSON array. No other text.';
}
async function fetchModels(inputId, hostId, listId, btnId) {
  const hostEl = $("#" + hostId);
  const listEl = $("#" + listId);
  const btn = $("#" + btnId);
  const host = hostEl ? hostEl.value.trim() : "";
  if (btn) { btn.disabled = true; btn.textContent = "fetching…"; }
  try {
    const keyId = inputId === "st-model" ? "st-key" : null;
    const key = keyId ? $("#" + keyId).value.trim() : "";
    const data = await api("/api/models?host=" + encodeURIComponent(host)
      + (key ? "&api_key=" + encodeURIComponent(key) : ""));
    const names = data.models || [];
    if (!names.length) throw new Error(data.error || "server returned no models");
    listEl.innerHTML = names.map(m => `<option value="${esc(m)}">`).join("");
    const input = $("#" + inputId);
    if (input && !input.value.trim() && names.length === 1) input.value = names[0];
    if (btn) btn.textContent = `${names.length} models`;
  } catch (e) {
    alert("Could not fetch models: " + e.message);
    if (btn) btn.textContent = "fetch models";
  } finally {
    if (btn) btn.disabled = false;
    if (btn && !btn.textContent.includes("models")) btn.textContent = "fetch models";
  }
}
function saveStrategy() {
  const name = $("#st-name").value.trim();
  if (!name) return alert("Give the strategy a name.");
  const model = $("#st-model").value.trim();
  if (!model) return alert("A strategy needs a model.");
  const cfg = {
    host: $("#st-host").value.trim() || "http://localhost:11434",
    model,
    api_key: $("#st-key").value.trim() || null,
    prompt: $("#st-prompt").value.trim(),
  };
  api("/api/strategies", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({name, config: cfg})})
    .then(loadConfigs)
    .catch(e => alert("Save failed: " + e.message));
}

function filterFormFields() {
  $("#filter-form").innerHTML = `
    <div class="field"><label for="fl-name">Name</label>
      <input id="fl-name" placeholder="e.g. security">
      <div class="hint">File name without extension — becomes
        <code>~/.config/ctools/filters/&lt;name&gt;.json</code>.</div></div>
    <div class="field"><label for="fl-backend">Backend</label>
      <select id="fl-backend" onchange="filterBackend()">
        <option value="decision">Decision model — local classifier per concept</option>
        <option value="builtin">Built-in — type / substring rules, no server</option>
        <option value="jsonrpc">JSON-RPC — your own subprocess</option>
      </select>
      <div class="hint" id="fl-backend-hint"></div></div>
    <div id="fl-body"></div>`;
  filterBackend();
}
function filterBackend() {
  const b = $("#fl-backend").value;
  const hint = $("#fl-backend-hint");
  if (b === "decision") {
    hint.textContent = "A local model (e.g. systemone on Ollama) answers yes/no or picks a label for every concept; concepts passing the threshold are kept.";
    $("#fl-body").innerHTML = `
      <div class="fgrid">
        <div class="field"><label for="fl-host">Server host</label>
          <input id="fl-host" value="http://localhost:11434">
          <div class="hint">Ollama default is <code>http://localhost:11434</code>.</div></div>
        <div class="field"><label for="fl-model">Model</label>
          <div class="modelrow">
            <input id="fl-model" list="fl-models" placeholder="e.g. tev1">
            <button type="button" id="fl-fetch" onclick="fetchModels('fl-model','fl-host','fl-models','fl-fetch')">fetch models</button>
          </div>
          <datalist id="fl-models"></datalist>
          <div class="hint">A trained decision model on that server.
            “fetch models” lists what the server has, or use <code>ollama list</code>.</div></div>
        <div class="field"><label for="fl-endpoint">Endpoint</label>
          <select id="fl-endpoint">
            <option value="systemone">systemone (typed, Ollama)</option>
            <option value="chat">chat (OpenAI-compatible)</option>
          </select>
          <div class="hint">How the model is queried.</div></div>
        <div class="field"><label for="fl-mode">Mode</label>
          <select id="fl-mode">
            <option value="noul">noul — yes / no</option>
            <option value="choice">choice — routing / labels</option>
          </select>
          <div class="hint">noul keeps concepts the model says “yes” to.
            choice routes them into categories.</div></div>
      </div>
      <div class="field"><label for="fl-instr">Instructions</label>
        <textarea id="fl-instr" rows="3" placeholder='e.g. Is this concept about security?'></textarea>
        <div class="hint">The question asked for each concept. Be specific —
          “Is this a hard requirement the implementation must satisfy?”</div></div>
      <div class="field"><label for="fl-thresh">Threshold <span>0–1, noul/systemone only</span></label>
        <input id="fl-thresh" type="number" step="0.05" min="0" max="1" value="0.6">
        <div class="hint">Confidence needed to keep a concept. 0.6 is a safe start;
          raise to drop borderline matches, lower to keep more.</div></div>`;
  } else if (b === "builtin") {
    hint.textContent = "No model, no server — pure code. Concepts pass when their type matches (and their text contains the required substring, if set).";
    $("#fl-body").innerHTML = `
      <div class="field"><label for="fl-types">Keep types <span>comma-separated</span></label>
        <input id="fl-types" placeholder="e.g. constraint,goal">
        <div class="hint">Only concepts with these types pass. Types come from the
          extraction prompt — the default uses: goal, constraint, decision,
          question, action, risk, entity. Leave empty to keep any type.</div></div>
      <div class="field"><label for="fl-excl">Exclude types <span>comma-separated</span></label>
        <input id="fl-excl" placeholder="e.g. noise">
        <div class="hint">Types that never pass, even if listed above.</div></div>
      <div class="field"><label for="fl-prompt">Required substring <span>optional</span></label>
        <input id="fl-prompt" placeholder="e.g. ssl">
        <div class="hint">Concept text must contain this string (case-insensitive).
          Combine with types for precise filters.</div></div>`;
  } else {
    hint.textContent = "Spawns your own program that speaks JSON-RPC over stdin/stdout (ctools sends classify/notify calls). Power-user option — see the example below the form.";
    $("#fl-body").innerHTML = `
      <div class="field"><label for="fl-cmd">Command <span>space-separated</span></label>
        <input id="fl-cmd" placeholder="e.g. python ~/.config/ctools/filters/myfilter.py">
        <div class="hint">The executable to spawn, with arguments. It must implement
          the JSON-RPC method below on stdin/stdout.</div></div>
      <div class="fgrid">
        <div class="field"><label for="fl-method">RPC method</label>
          <input id="fl-method" value="classify">
          <div class="hint">Method name your program handles.</div></div>
        <div class="field"><label for="fl-timeout">Timeout <span>seconds</span></label>
          <input id="fl-timeout" type="number" value="30">
          <div class="hint">Give up waiting for your program after this long.</div></div>
      </div>`;
  }
}
function saveFilter() {
  const name = $("#fl-name").value.trim();
  if (!name) return alert("Give the filter a name.");
  const b = $("#fl-backend").value;
  const g = id => { const e = $(id); return e ? e.value.trim() : ""; };
  let cfg = {};
  if (b === "decision") {
    if (!g("#fl-model")) return alert("A decision filter needs a model.");
    if (!g("#fl-instr")) return alert("A decision filter needs instructions.");
    cfg = { type: "systemone", host: g("#fl-host") || "http://localhost:11434",
      model: g("#fl-model"), endpoint: g("#fl-endpoint") || "systemone",
      instructions: g("#fl-instr"), mode: g("#fl-mode") || "noul",
      threshold: parseFloat(g("#fl-thresh") || "0.6") };
  } else if (b === "builtin") {
    const types = g("#fl-types").split(",").map(s=>s.trim()).filter(Boolean);
    const excl = g("#fl-excl").split(",").map(s=>s.trim()).filter(Boolean);
    cfg = {};
    if (types.length) cfg.types = types;
    if (excl.length) cfg.exclude_types = excl;
    if (g("#fl-prompt")) cfg.prompt = g("#fl-prompt");
    if (!Object.keys(cfg).length) cfg.types = [];
  } else {
    if (!g("#fl-cmd")) return alert("A JSON-RPC filter needs a command.");
    const parts = g("#fl-cmd").split(/\s+/);
    cfg = { type: "jsonrpc", command: parts,
      method: g("#fl-method") || "classify",
      timeout: parseFloat(g("#fl-timeout") || "30") };
  }
  api("/api/filters", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({name, config: cfg})})
    .then(loadConfigs)
    .catch(e => alert("Save failed: " + e.message));
}

// --- Concept extraction with strategy + filter selection -----------------
let EXTRACTION = {strategy: null, filter: null};

async function showConcepts() {
  if (!CONV) return;
  const {agent, session_id: sid} = CONV;
  const box = document.createElement("div");
  box.id = "concept-box";
  // Controls
  const stratOpts = `<option value="">none (regex)</option>` +
    STRATEGIES.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  const filterOpts = `<option value="">none</option>` +
    FILTERS.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  box.innerHTML = `
    <div class="toolbar">
      <select id="ex-strat">${stratOpts}</select>
      <select id="ex-filter">${filterOpts}</select>
      <button class="primary" onclick="runExtraction()">Extract</button>
    </div>
    <p class="dim small">Strategy = how to chunk concepts (LLM). Filter = which
      concepts pass through (decision model, built-in, or JSON-RPC).</p>
    <div id="ex-results"></div>`;
  $("#c-body").prepend(box);
  $("#ex-strat").value = EXTRACTION.strategy || "";
  $("#ex-filter").value = EXTRACTION.filter || "";
  runExtraction();
}

async function runExtraction() {
  if (!CONV) return;
  const {agent, session_id: sid} = CONV;
  const strat = $("#ex-strat") ? $("#ex-strat").value : "";
  const flt = $("#ex-filter") ? $("#ex-filter").value : "";
  EXTRACTION = {strategy: strat || null, filter: flt || null};
  const res = $("#ex-results");
  res.innerHTML = `<p class="dim">Extracting${strat ? " with strategy “" + esc(strat) + "”" : ""}${flt ? " + filter “" + esc(flt) + "”" : ""}…</p>`;
  let qs = "?";
  if (strat) qs += "strategy=" + encodeURIComponent(strat) + "&";
  if (flt) qs += "filter=" + encodeURIComponent(flt);
  try {
    const d = await api(`/api/agents/${encodeURIComponent(agent)}/sessions/${encodeURIComponent(sid)}/concepts${qs}`);
    if (!d.concepts.length) {
      res.innerHTML = `<div class="notice" style="color:var(--dim)">No concepts found.
        ${flt ? "The filter may have dropped everything." : ""}</div>`;
      return;
    }
    res.innerHTML = d.concepts.map(c => `
      <div class="concept">
        <span class="type">${esc(c.type || "concept")}</span>
        <pre>${esc(c.short || c.medium || c.long || c.description || JSON.stringify(c))}</pre>
      </div>`).join("");
  } catch (e) {
    res.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

loadOverview().then(applyRoute).catch(e =>
  document.body.innerHTML = `<div class="err" style="margin:40px">${esc(e.message)}</div>`);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Startup logo

_FALLBACK_LOGO = (
    "░█▀▀░▀█▀░█▀█░█▀█░█░░░█▀▀\n"
    "  ░█░░░░█░░█░█░█░█░█░░░▀▀█\n"
    "  ░▀▀▀░░▀░░▀▀▀░▀▀▀░▀▀▀░▀▀▀"
)


def _load_logo_art() -> str:
    """Return the block-art lines (footer placeholder lines stripped).

    Reads ``logo.txt`` that ships alongside the package; falls back to an
    embedded copy if the file is missing or unreadable.
    """
    import os
    candidates = []
    try:
        import ctools
        candidates.append(os.path.join(os.path.dirname(ctools.__file__), "..", "logo.txt"))
    except Exception:
        pass
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logo.txt"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read().rstrip("\n")
        except OSError:
            continue
        # Drop the placeholder footer (version / url) so we can re-render it
        # with live values, and stop at the first blank line after the art.
        art = []
        for line in text.splitlines():
            if "version (" in line or "http://" in line:
                continue
            art.append(line)
        # Trim trailing blank lines.
        while art and not art[-1].strip():
            art.pop()
        if any(l.strip() for l in art):
            return "\n".join(art)
    return _FALLBACK_LOGO


def _banner_lines(art: str, host: str, port: int):
    """Return (art_rows, footer_lines) ready to render."""
    raw = art.splitlines()
    art_rows = [ln for ln in raw if ln.strip() != ""]
    # Equalize width so per-column shimmer math is uniform.
    width = max(len(ln) for ln in art_rows)
    art_rows = [ln.ljust(width) for ln in art_rows]
    footer = [f"       version {__version__}", f"   http://localhost:{port}"]
    return art_rows, footer


def _render_banner(host: str, port: int, animate: bool) -> None:
    """Print the ctools banner. If ``animate`` and stdout is a TTY, sweep a
    fast left-to-right shimmer across the block art before settling."""
    import time as _t
    import os

    art_rows, footer = _banner_lines(_load_logo_art(), host, port)
    if not art_rows:
        print(f"ctools {__version__} dashboard: http://{host}:{port}")
        return

    def static_block() -> None:
        print()  # one blank line above the logo
        for ln in art_rows:
            print(ln)
        print()  # one blank line below the logo
        for ln in footer:
            print(ln)

    if not animate or not sys.stdout.isatty():
        static_block()
        return

    # ---- animated shimmer -------------------------------------------------
    cols = os.environ.get("COLORTERM", "")
    truecolor = "truecolor" in cols.lower() or "24bit" in cols

    # The logo is a LIGHT base; a DARK band sweeps across it. `phase` below
    # means "how dark" (1.0 = darkest right at the sweep head, 0.0 = full
    # bright white). So the logo reads bright with a shadow shimmering over it.
    if truecolor:
        def col_color(phase: float) -> str:
            if phase > 0.85:      # darkest core of the sweep
                r, g, b = 25, 35, 55
            elif phase > 0.55:    # mid shadow
                r, g, b = 90, 120, 165
            else:                 # near-bright, blending back to the logo
                r, g, b = 200, 220, 245
            return f"\x1b[38;2;{r};{g};{b}m"
    else:  # 256-color fallback
        def col_color(phase: float) -> str:
            if phase > 0.85:
                return "\x1b[34m"        # dim blue (dark core)
            if phase > 0.55:
                return "\x1b[96m"        # bright cyan (mid)
            return "\x1b[97m"            # bright white (base)

    reset = "\x1b[0m"
    n = len(art_rows[0])
    rows = len(art_rows)
    frames = 18
    # Each frame block is: one blank line + the art rows. So the on-screen
    # block height is (rows + 1); the cursor-up on the next frame must match.
    block_height = rows + 1

    # A single bright color for the settled (final) frame, per color mode.
    if truecolor:
        settle_color = "\x1b[38;2;235;245;255m"   # bright white
    else:
        settle_color = "\x1b[97m"                 # bright white

    for f in range(frames):
        last = (f == frames - 1)
        head = (f / max(1, frames - 1)) * (n + 10) - 5
        body = []
        for ln in art_rows:
            if last:
                # Final frame: the logo, in one bright static color. This IS
                # the logo — no secondary copy is printed afterwards.
                body.append(settle_color + ln + reset)
                continue
            out = []
            for i, ch in enumerate(ln):
                if ch == " ":
                    out.append(ch)
                    continue
                dist = abs(i - head)
                phase = max(0.0, 1.0 - dist / 12.0)
                out.append(col_color(phase) + ch)
            body.append("".join(out) + reset)
        if f:
            # Cursor is currently one line below the last art row (the \n
            # after each frame parks it there). Move up exactly `block_height`
            # lines to the leading blank line, then erase below so no ghost
            # rows remain.
            sys.stdout.write(f"\x1b[{block_height}A\x1b[J")
        sys.stdout.write("\n" + "\n".join(body) + "\n")
        sys.stdout.flush()
        if last:
            break
        _t.sleep(0.03)

    # The shimmer's final frame already rendered the logo. Add one blank
    # line below it, then the footer (version / url).
    print()
    for ln in footer:
        print(ln)


# ---------------------------------------------------------------------------
# Entry point

def main():
    parser = argparse.ArgumentParser(description="ctools web dashboard")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1 — local only)")
    parser.add_argument("--no-logo", action="store_true",
                        help="skip the animated startup banner")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"

    if not args.no_logo:
        _render_banner(args.host, args.port, animate=True)
    else:
        print(f"ctools {__version__} dashboard: {url}")
    print("      Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
        server.shutdown()


if __name__ == "__main__":
    main()
