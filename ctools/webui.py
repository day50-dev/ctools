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
import threading
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
from ctools.filterlib import load_filter
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
    flags = re.IGNORECASE if ignore_case else 0
    # Regex first (grep-style); if the pattern isn't a valid regex, fall back
    # to a literal substring so the navbar search box "just works" for plain
    # text. The response says which mode ran.
    mode = "regex"
    try:
        compiled = _compile_pattern(pattern, flags)
    except re.error:
        mode = "literal"
        compiled = re.compile(re.escape(pattern), flags)

    if agents == ["*"]:
        targets = list(REGISTRY.values())
    else:
        targets = []
        for name in agents:
            agent = get_agent(name)
            if agent is None:
                return None, f"Unknown agent: {name}"
            targets.append(agent)

    # Pass 1 — session metadata (id, name, path). Cheap: session objects only,
    # no line scanning. Independent of the content cap below.
    session_hits = []
    agent_sessions = {}
    for agent in targets:
        if not agent.exists():
            continue
        try:
            sessions = agent.sessions()
        except AgentError:
            continue
        agent_sessions[agent.name] = sessions
        for session in sessions:
            hay = "\n".join(filter(None, [
                session.id, session.name or "", getattr(session, "path", "") or ""]))
            if hay and compiled.search(hay):
                session_hits.append({
                    "agent": agent.name,
                    "session_id": session.id,
                    "name": session.name or "",
                    "path": getattr(session, "path", "") or "",
                    "mtime": session.mtime.isoformat() if session.mtime else None,
                    "message_count": session.message_count or 0,
                })
    session_hits.sort(key=lambda s: s["mtime"] or "", reverse=True)
    session_hits = session_hits[:200]

    # Pass 2 — conversation content, capped.
    matches = []
    per_agent = {}
    errors = []
    for agent in targets:
        if not agent.exists():
            continue
        sessions = agent_sessions.get(agent.name)
        if sessions is None:
            errors.append(f"{agent.name}: sessions unreadable")
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

    result = {
        "matches": matches, "total": len(matches), "per_agent": per_agent,
        "errors": errors, "sessions": session_hits,
        "sessions_total": len(session_hits), "mode": mode,
    }
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

    def _filter_payload(self):
        """Show exactly what a filter would send, for a sample concept.

        Reads the in-progress filter form values from the query string and
        builds the real outbound payload (no network call). For decision
        models this is the systemone or chat request body; for built-in and
        JSON-RPC it's a rule/method summary. Returns
        ``{backend, description, example: {...}}``.
        """
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else {})
        g = lambda k, d="": (q.get(k, [d])[0] or d).strip()
        backend = g("backend", "decision")
        sample = g("sample", "Is this concept about security?") or "Is this concept about security?"

        if backend == "decision":
            model = g("model")
            instructions = g("instructions")
            if not model or not instructions:
                return self._error(400, "Set a model and instructions to preview the payload.")
            cfg = {
                "type": "systemone",
                "host": g("host", "http://localhost:11434") or "http://localhost:11434",
                "model": model,
                "endpoint": g("endpoint", "systemone") or "systemone",
                "instructions": instructions,
                "mode": g("mode", "noul") or "noul",
                "threshold": float(g("threshold", "0.6") or "0.6"),
            }
            try:
                f = load_filter(cfg)
            except Exception as e:
                return self._error(400, f"Invalid decision filter: {e}")
            ex = f.example_payload(sample)
            note = ("The model returns a calibrated probability per option; "
                    f"the concept is kept when {cfg['mode']} >= threshold."
                    if cfg["endpoint"] == "systemone" else
                    "The typed question is rendered into a lettered prompt and the "
                    "model's lettered reply is parsed. No probabilities — only the "
                    "chosen option is available.")
            return self._json({"backend": "decision", "description": note,
                               "example": ex})

        if backend == "builtin":
            types = [t for t in g("types").split(",") if t.strip()]
            excl = [t for t in g("exclude").split(",") if t.strip()]
            prompt = g("prompt")
            rule = {}
            if types: rule["types"] = types
            if excl: rule["exclude_types"] = excl
            if prompt: rule["prompt"] = prompt
            # Walk a couple of sample concepts through the real logic.
            samples = [
                {"type": types[0] if types else "goal", "short": sample},
                {"type": "noise", "short": "unrelated"},
            ]
            results = []
            for c in samples:
                ct = c["type"]
                short = c["short"]
                kept = True; reason = "kept"
                if types and ct not in types:
                    kept = False; reason = f"type {ct!r} not in {types}"
                elif excl and ct in excl:
                    kept = False; reason = f"type {ct!r} excluded"
                elif prompt and prompt.lower() not in short.lower() and prompt.lower() not in c.get("description"," ").lower():
                    kept = False; reason = f"no match for {prompt!r}"
                results.append({"concept": {"type": ct, "short": short},
                                "kept": kept, "reason": reason})
            return self._json({"backend": "builtin",
                               "description": "Pure code — no server. Each concept is kept when its type is allowed and its text matches.",
                               "example": {"rules": rule, "sample_results": results}})

        if backend == "jsonrpc":
            cmd = [c for c in g("command").split() if c]
            if not cmd:
                return self._error(400, "Set a command to preview the payload.")
            return self._json({"backend": "jsonrpc",
                               "description": "Your subprocess is spawned and asked over JSON-RPC on stdin/stdout.",
                               "example": {"command": cmd,
                                           "method": g("method", "classify") or "classify",
                                           "call": {"jsonrpc": "2.0", "id": 1,
                                                     "method": g("method", "classify") or "classify",
                                                     "params": {"text": sample}}}})

        return self._error(400, f"Unknown backend: {backend}")

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
            token = getattr(Handler, "_shutdown_token", "") or ""
            page = INDEX_HTML.replace("__SHUTDOWN_TOKEN__", token)
            return self._send(200, page.encode("utf-8"),
                              "text/html; charset=utf-8")

        if parts[0] == "api":
            return self.api_get(parts[1:])
        return self._error(404, "Not found")

    def api_get(self, parts):
        if parts == ["models"]:
            return self._get_models()

        if parts == ["filter-payload"]:
            return self._filter_payload()

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
            explain = q.get("explain", [""])[0] == "1"
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
                    # session_concepts takes a PATH and reads it itself.
                    filter_config = str(fp)
            if session_concepts is None:
                return self._error(503, "Concept extraction unavailable (ctools.cextract failed to import).")
            trace = {} if explain else None
            try:
                concepts = session_concepts(agent, sid, strategy, filter_config,
                                            trace=trace)
            except AgentError as e:
                return self._error(404, str(e))
            except SystemExit as e:
                return self._error(400, f"{e}")
            except Exception as e:
                return self._error(500, f"Concept extraction failed: {e}")
            if concept_type:
                concepts = [c for c in concepts if c.get("type") == concept_type]
            result = {"agent": agent.name, "session_id": sid,
                      "concepts": concepts}
            if explain and trace is not None:
                # The trace stores the filter's path; show its name instead.
                if filter_name and isinstance(trace.get("filter"), dict):
                    trace["filter"]["name"] = filter_name
                result["pipeline"] = trace
            return self._json(result)

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

    def _do_shutdown(self, body):
        """Cleanly stop the server from inside the page.

        Requires the per-process token (embedded in the page HTML at startup)
        so a stray request or a different instance can't kill this server.
        Responds first, then shuts down after a short delay so the client
        gets the confirmation before the socket closes.
        """
        token = body.get("token")
        expected = getattr(Handler, "_shutdown_token", None)
        server = getattr(Handler, "_server", None)
        if not expected or not server:
            return self._error(503, "Shutdown not available.")
        if not isinstance(token, str) or token != expected:
            return self._error(403, "Invalid or missing shutdown token.")
        self._json({"ok": True, "message": "Shutting down.", "delay": 0.5})

        def _later():
            import time
            time.sleep(0.5)  # let the response flush
            print("\nbye.")
            try:
                server.shutdown()
            except Exception:
                pass

        threading.Thread(target=_later, daemon=True).start()
        return None

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

        if parts[1:] == ["shutdown"]:
            return self._do_shutdown(body)

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
  --sans: system-ui, -apple-system, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
/* initial-load overlay: shown until the first agent scan finishes */
.bootload { position: fixed; inset: 0; background: var(--bg); z-index: 50;
  display: flex; flex-direction: column; align-items: center; justify-content:
  center; gap: 18px; transition: opacity .3s ease; }
.bootload.gone { opacity: 0; pointer-events: none; }
.bootload .spin { width: 34px; height: 34px; border: 3px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%; animation: spin .8s
  linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.bootload .bootmsg { color: var(--dim); font-size: 14px; letter-spacing: .2px; }
/* lightweight inline spinner for per-view fetches */
.loading { display: inline-flex; align-items: center; gap: 8px; color: var(--dim);
  font-size: 13px; padding: 10px 0; }
.loading .spin { width: 15px; height: 15px; border: 2px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%; animation: spin .8s
  linear infinite; }
header { display: flex; align-items: center; gap: 14px; padding: 12px 22px;
  border-bottom: 1px solid var(--border); background: var(--panel);
  position: sticky; top: 0; z-index: 5; flex-wrap: wrap; }
header h1 { font-size: 17px; margin: 0; font-weight: 700; letter-spacing: .3px; cursor: pointer; }
header h1:hover { color: var(--accent); }
header h1 span { color: var(--accent); }
.navsearch { display: flex; align-items: center; gap: 4px; margin-left: auto;
  background: var(--bg); border: 1px solid var(--border); border-radius: 9px;
  padding: 3px 6px 3px 10px; flex: 0 1 340px; min-width: 170px; }
.navsearch:focus-within { border-color: var(--accent); }
.navsearch input[type=search] { flex: 1; min-width: 0; font-size: 13px; padding: 5px 0;
  background: none; border: none; }
.navsearch .ic { display: flex; align-items: center; gap: 3px; font-size: 11px;
  color: var(--dim); cursor: pointer; user-select: none; white-space: nowrap; }
.navsearch .ic input { width: auto; margin: 0; }
.navsearch button { background: none; border: none; color: var(--dim); font-size: 15px;
  cursor: pointer; padding: 4px 6px; border-radius: 6px; line-height: 1; }
.navsearch button:hover { color: var(--accent); background: var(--panel2); }
nav { display: flex; gap: 4px; }
nav button { background: none; border: 1px solid transparent; color: var(--dim);
  padding: 6px 12px; border-radius: 8px; cursor: pointer; font-size: 13px; }
nav button:hover { color: var(--text); background: var(--panel2); }
nav button.on { color: var(--text); background: var(--panel2);
  border-color: var(--border); }
.power { margin-left: 8px; background: none; border: 1px solid var(--border);
  color: var(--dim); width: 34px; height: 34px; border-radius: 9px; cursor: pointer;
  font-size: 16px; line-height: 1; flex: 0 0 auto; }
.power:hover { color: var(--red); border-color: var(--red); background: var(--panel2); }
.power:disabled { opacity: .5; cursor: default; }
main { padding: 20px 22px 60px; max-width: 1100px; margin: 0 auto; }
.view { display: none; } .view.on { display: block; }
/* the Concepts expansion lives in its own labeled container so it reads as a
   tool/panel, distinct from the .msg conversation cards below it */
#concept-box { border: 1px solid var(--border); border-top: 3px solid var(--accent);
  border-radius: 12px; background: var(--panel); margin-bottom: 24px;
  overflow: hidden; }
#concept-box .cp-head { display: flex; align-items: baseline; gap: 10px;
  padding: 10px 16px; background: var(--panel2); border-bottom: 1px solid var(--border); }
#concept-box .cp-title { font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .8px; color: var(--accent); }
#concept-box .cp-sub { font-size: 11px; color: var(--dim); }
#concept-box .cp-body { padding: 14px 16px; }
/* first-time onboarding card in the Concepts panel (conversation view) */
.onboard { background: var(--panel2); border: 1px solid var(--border);
  border-left: 3px solid var(--accent); border-radius: 10px; padding: 12px 14px;
  font-size: 13px; line-height: 1.55; color: var(--text); margin-bottom: 14px; }
.onboard b { color: var(--text); }
.onboard-link { display: inline-block; margin-top: 8px; color: var(--accent);
  font-weight: 600; text-decoration: none; cursor: pointer; }
.onboard-link:hover { text-decoration: underline; }
/* highlight a section card when jumped to from the onboarding link */
.flash { animation: flash 2.4s ease; }
@keyframes flash {
  0%, 100% { box-shadow: 0 0 0 0 rgba(90,169,255,0); }
  12%, 48% { box-shadow: 0 0 0 3px rgba(90,169,255,.55); }
}
/* --- concepts (configs) page: a light vertical flow --- */
.cfg { max-width: 760px; }
.diagram { max-width: 760px; background: var(--panel);
  border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px;
  margin: 18px 0 30px; }
.diagram svg { display: block; width: 100%; height: auto; }
.diagram .dlabel { font-size: 13px; font-weight: 700; fill: var(--text);
  text-anchor: middle; font-family: var(--sans, sans-serif); }
.diagram .ddim { font-size: 11px; fill: var(--dim); text-anchor: middle;
  font-family: var(--sans, sans-serif); }
.diagram .dout { font-size: 12px; font-weight: 700; fill: var(--bg);
  text-anchor: middle; font-family: var(--sans, sans-serif); }
.diagram .dor { font-size: 13px; fill: var(--accent); text-anchor: middle;
  font-family: var(--mono); font-style: italic; }
.cfg-step { background: var(--panel); border: 1px solid var(--border);
  border-radius: 14px; padding: 22px 26px; }
.cfg-gap { height: 26px; }
.cfg-head { display: flex; gap: 14px; align-items: flex-start; }
.cfg-head h3 { margin: 0 0 5px; font-size: 15px; color: var(--text); font-weight: 700; }
.cfg-head p { margin: 0; line-height: 1.55; }
.cfg-step h4 { margin: 24px 0 10px; font-size: 12px; text-transform: uppercase;
  letter-spacing: .8px; color: var(--dim); font-weight: 600; }
/* live "under the hood" payload preview */
.payloadbox { margin-top: 18px; border: 1px dashed var(--border); border-radius: 10px;
  padding: 12px 14px; background: var(--bg); }
.payloadbox .payload-head { font-size: 11px; font-weight: 700; letter-spacing: .8px;
  text-transform: uppercase; color: var(--dim); margin-bottom: 6px; }
.payloadbox .payload-desc { margin: 0 0 8px; font-size: 12.5px; color: var(--text);
  line-height: 1.5; }
.payloadbox .payload-pre { margin: 0; background: var(--panel); border: 1px solid
  var(--border); border-radius: 8px; padding: 12px 14px; font-family: var(--mono);
  font-size: 12px; white-space: pre-wrap; max-height: 320px; overflow: auto;
  color: var(--text); }
h2 { font-size: 15px; margin: 22px 0 10px; color: var(--dim);
  text-transform: uppercase; letter-spacing: .8px; font-weight: 600; }
h2:first-child { margin-top: 0; }
.crumb { color: var(--accent); text-decoration: none; cursor: pointer; }
.crumb:hover { text-decoration: underline; }
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
/* On phones, drop the search next to the brand (it keeps the auto-margin /
   right-alignment on wider screens). */
@media (max-width: 720px) { .navsearch { margin-left: 0; flex: 1 1 auto; min-width: 0; } }
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
.pipe { margin: 16px 0 20px; }
.pipe .stage { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 14px; }
.pipe .stage-head { font-size: 11px; font-weight: 700; letter-spacing: .5px;
  text-transform: uppercase; color: var(--accent); }
.pipe .stage-body { font-size: 13px; margin-top: 5px; line-height: 1.5; }
.pipe .stage-body code { font-family: var(--mono); font-size: 12px;
  background: var(--panel2); padding: 1px 5px; border-radius: 4px; }
.pipe .stage details { margin-top: 8px; }
.pipe .stage summary { cursor: pointer; font-size: 12px; color: var(--dim);
  font-family: var(--mono); }
.pipe .stage summary:hover { color: var(--text); }
.pipe .pipe-pre { margin: 6px 0 0; padding: 10px 12px; background: var(--bg);
  border: 1px solid var(--border); border-radius: 6px; font-family: var(--mono);
  font-size: 12px; white-space: pre-wrap; max-height: 300px; overflow: auto; }
.pipe .pipe-arrow { text-align: center; color: var(--dim); font-size: 11px;
  padding: 3px 0; line-height: 1; }
.pipe .droplist { margin: 6px 0 0; padding-left: 18px; font-size: 12px;
  line-height: 1.7; }
.pipe .droplist li { margin: 2px 0; }
.pipe .droplist .dim { color: var(--dim); }
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
<div class="bootload" id="bootload">
  <div class="spin"></div>
  <div class="bootmsg">scanning your agents…</div>
</div>
<header>
  <h1>ctools<span>.</span></h1>
  <div class="navsearch" title="Search sessions and conversations — / to focus, Ctrl+K also works">
    <input type="search" id="q" placeholder="search sessions & conversations…" autocomplete="off" spellcheck="false">
    <button id="qbtn" title="Search (Enter)" aria-label="Search">⌕</button>
  </div>
  <nav>
    <button data-view="overview">Overview</button>
    <button data-view="sessions">Sessions</button>
    <button data-view="configs">Concepts</button>
  </nav>
  <button id="power" class="power" title="Shut down the dashboard" aria-label="Shut down"
          onclick="shutdown()">⏻</button>
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
    <h2>Search</h2>
    <p class="dim small" id="q-intro">Type a pattern in the bar above (or press <b>/</b>) — it searches
      session ids, names, paths <b>and</b> the content of every conversation on this machine,
      all at once. Then filter the results below.</p>
    <div class="toolbar">
      <div id="q-agents"></div>
      <div id="q-types" style="display:flex;gap:6px;flex-wrap:wrap">
        <span class="chip on" data-t="all">everything</span>
        <span class="chip" data-t="sessions">sessions only</span>
        <span class="chip" data-t="matches">matches only</span>
      </div>
    </div>
    <p id="q-summary" class="small dim"></p>
    <div id="q-sessions"></div>
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
    <h2>Concepts</h2>
    <p class="dim small">The <b>Concepts</b> panel turns a conversation into
      concepts. <b>Strategies</b> and <b>filters</b> are two independent options
      on that path — use either, both, or neither.</p>

    <div class="diagram">
      <svg viewBox="0 0 760 300" width="100%" height="300" role="img"
           aria-label="Conversation, then a strategy or the built-in regex extracts concepts, then an optional filter keeps a subset, giving the final concepts.">
        <defs>
          <marker id="arw" markerWidth="9" markerHeight="9" refX="7" refY="4.5"
                  orient="auto" markerUnits="userSpaceOnUse">
            <path d="M0,0 L8,4.5 L0,9 Z" fill="var(--dim)"></path>
          </marker>
        </defs>
        <!-- input -->
        <g class="dnode">
          <rect x="16" y="118" width="150" height="64" rx="12"
                fill="var(--panel2)" stroke="var(--border)"></rect>
          <text x="91" y="146" class="dlabel">conversation</text>
          <text x="91" y="164" class="ddim">your session</text>
        </g>
        <!-- two extraction options, stacked -->
        <g class="dnode">
          <rect x="250" y="40" width="190" height="64" rx="12"
                fill="var(--panel)" stroke="var(--accent)"></rect>
          <text x="345" y="68" class="dlabel">strategy <tspan class="ddim">(LLM)</tspan></text>
          <text x="345" y="86" class="ddim">a saved prompt + model</text>
        </g>
        <g class="dnode">
          <rect x="250" y="196" width="190" height="64" rx="12"
                fill="var(--panel)" stroke="var(--border)"></rect>
          <text x="345" y="224" class="dlabel">built-in regex</text>
          <text x="345" y="242" class="ddim">“none” — no model</text>
        </g>
        <!-- filter (optional) -->
        <g class="dnode">
          <rect x="510" y="118" width="150" height="64" rx="12"
                fill="var(--panel)" stroke="var(--border)" stroke-dasharray="5 4"></rect>
          <text x="585" y="146" class="dlabel">filter</text>
          <text x="585" y="164" class="ddim">optional · keep / drop</text>
        </g>
        <!-- output -->
        <g class="dnode">
          <rect x="678" y="118" width="78" height="64" rx="12"
                fill="var(--green)" stroke="none"></rect>
          <text x="717" y="155" class="dout">concepts</text>
        </g>
        <!-- connectors -->
        <g class="dlink" stroke="var(--dim)" fill="none" stroke-width="1.5">
          <path d="M166,144 C 200,144 210,72 248,72" marker-end="url(#arw)"></path>
          <path d="M166,160 C 200,160 210,228 248,228" marker-end="url(#arw)"></path>
          <path d="M440,72 C 475,72 470,140 508,140" marker-end="url(#arw)"></path>
          <path d="M440,228 C 475,228 470,160 508,160" marker-end="url(#arw)"></path>
          <path d="M660,150 L 674,150" marker-end="url(#arw)"></path>
        </g>
        <!-- the “or” between the two extraction paths -->
        <text x="345" y="150" class="dor">or</text>
        <!-- the “optional” skip line from extraction straight to concepts -->
        <path d="M470,150 L 508,150" class="dskip" stroke="var(--dim)"
              stroke-dasharray="2 4" fill="none" stroke-width="1.2"
              marker-end="url(#arw)"></path>
      </svg>
    </div>

    <div class="cfg">
      <div class="cfg-step">
        <div class="cfg-head">
          <div>
            <h3>Strategies <span class="dim small">— extraction</span></h3>
            <p class="dim small">Point an LLM at a conversation to chunk it into
              concepts. Or leave the panel on <b>none (regex)</b> for built-in
              pattern matching — no model, no server.</p>
          </div>
        </div>
        <div id="strat-list" class="grid" style="margin:16px 0 18px"></div>
        <details class="examplebox"><summary>Show example JSON</summary>
<pre>{
  "host": "http://localhost:11434",   // any OpenAI-compatible server (Ollama default)
  "model": "qwen2.5:3b",              // model name on that server
  "api_key": null,                    // leave null for Ollama; set for OpenAI etc.
  "prompt": "Extract the key concepts..."  // instructions to the LLM
}</pre>
        </details>
        <h4>New strategy</h4>
        <div id="strat-form" style="margin-bottom:10px"></div>
        <button class="primary" onclick="saveStrategy()">Create strategy</button>
      </div>

      <div class="cfg-gap"></div>

      <div class="cfg-step">
        <div class="cfg-head">
          <div>
            <h3>Filters <span class="dim small">— keep / drop</span></h3>
            <p class="dim small">After extraction, choose which concepts pass
              through. Three backends: <b>decision model</b> (a local classifier),
              <b>built-in</b> (match by type &amp; substring — no server), or
              <b>JSON-RPC</b> (your own subprocess).</p>
          </div>
        </div>
        <div id="filter-list" class="grid" style="margin:16px 0 18px"></div>
        <details class="examplebox"><summary>Show example JSON</summary>
<pre>// built-in — no server: keep by type and/or substring
{
  "types": ["constraint", "goal"],
  "exclude_types": [],
  "prompt": "ssl"              // require this substring
}

// decision model — ask a local classifier per concept
{
  "type": "systemone",
  "model": "tev1",
  "instructions": "Is this about security?"
}

// JSON-RPC — your own subprocess over stdin/stdout
{
  "type": "jsonrpc",
  "command": ["python", "filter.py"],
  "method": "classify"
}</pre>
        </details>
        <h4>New filter</h4>
        <div id="filter-form" style="margin-bottom:10px"></div>
        <button class="primary" onclick="saveFilter()">Create filter</button>
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
const SHUTDOWN_TOKEN = "__SHUTDOWN_TOKEN__";

// Shut the dashboard down from the page. Asks first, then calls the token-
//guarded endpoint. The server confirms and stops ~0.5s later.
async function shutdown() {
  if (!confirm("Shut down the ctools dashboard?\n\nOpen conversations keep their data; this just stops the web server.")) return;
  const btn = $("#power");
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    await api("/api/shutdown", { method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: SHUTDOWN_TOKEN}) });
    document.body.innerHTML =
      `<div style="display:flex;align-items:center;justify-content:center;
        height:100vh;font-size:18px;color:var(--dim)">bye — the dashboard is stopping.</div>`;
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "⏻"; }
    alert("Could not shut down: " + e.message);
  }
}

function switchView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("on"));
  // A conversation is a drill-down from Sessions, so keep Sessions lit while
  // one is open — no nav button is left orphaned / nothing highlighted.
  const navName = (name === "converse") ? "sessions" : name;
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("on", b.dataset.view === navName));
  $("#view-" + name).classList.add("on");
}
// --- hash routing: #, #search, #sessions/<agent>[?sort=x], #session/<agent>/<sid>, #configs ---
function parseHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (!h) return {view: "overview"};
  const parts = h.split("/");
  if (parts[0] === "search" || parts[0].startsWith("search?")) {
    let q = null;
    const qi = parts[0].indexOf("?");
    if (qi >= 0) (parts[0].slice(qi + 1).split("&")).forEach(kv => {
      const [k, v] = kv.split("=");
      if (k === "q" && v !== "") q = v;
    });
    return {view: "search", q};
  }
  if (parts[0] === "configs" || parts[0].startsWith("configs?")) {
    let focus = null;
    const qi = parts[0].indexOf("?");
    if (qi >= 0) (parts[0].slice(qi + 1).split("&")).forEach(kv => {
      const [k, v] = kv.split("=");
      if (k === "focus" && (v === "strategy" || v === "filter")) focus = v;
    });
    return {view: "configs", focus};
  }
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

// Scroll a create-form into view and flash it — used when a first-time user
// follows the “set up your first …” link from the Concepts panel.
function focusForm(kind) {
  if (!kind) return;
  const el = document.getElementById(kind === "strategy" ? "strat-form" : "filter-form");
  if (!el) return;
  // flash the whole section card (the form's parent .cfg-step), not just the
  // form — it reads as “look at this section” rather than a small highlight
  const container = el.closest(".cfg-step") || el.parentElement;
  // small delay so the view has laid out before we scroll to it (robust in
  // background tabs / headless, where requestAnimationFrame may not fire)
  setTimeout(() => {
    el.scrollIntoView({behavior: "smooth", block: "center"});
    container.classList.add("flash");
    setTimeout(() => container.classList.remove("flash"), 2400);
    const inp = el.querySelector("input");
    if (inp) setTimeout(() => inp.focus({preventScroll: true}), 400);
  }, 60);
}

function applyRoute() {
  const r = parseHash();
  switchView(r.view);
  if (r.view === "configs") {
    if (!window._configsLoaded) {
      window._configsLoaded = true;
      loadConfigs().then(() => { stratFormFields(); filterFormFields(); focusForm(r.focus); });
    } else if (r.focus) {
      focusForm(r.focus);
    }
  }
  if (r.view === "sessions") {
    const known = [...$("#s-agent").options].some(o => o.value === r.agent);
    $("#s-agent").value = known ? r.agent : $("#s-agent").value;
    if (r.sort) $("#s-sort").value = r.sort;
    const key = $("#s-agent").value + "|" + $("#s-sort").value;
    if (key !== window._shownSessKey) { window._shownSessKey = key; listSessions(); }
  }
  if (r.view === "search") {
    if (r.q != null && r.q !== "") {
      if (r.q !== $("#q").value) $("#q").value = r.q;
      runGlobalSearch(); // fetches only if the pattern changed
    } else if (LAST_SEARCH) {
      renderSearch();
    }
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
      // chips filter the already-fetched results client-side — instant
      searchAgent = c.dataset.a;
      renderSearch();
    });
  document.querySelectorAll("#q-types .chip").forEach(c =>
    c.onclick = () => {
      document.querySelectorAll("#q-types .chip")
        .forEach(x => x.classList.remove("on"));
      c.classList.add("on");
      searchType = c.dataset.t;
      renderSearch();
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
  $("#s-result").innerHTML = `<div class="loading"><span class="spin"></span>loading sessions…</div>`;
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

// --- global search (navbar) ---------------------------------------------
// One query, everything at once: session metadata (id/name/path) AND
// conversation content. We fetch across ALL agents once, then filter
// (agent, result type) client-side so chips re-render instantly.
let LAST_SEARCH = null;   // {q, ic, matches, total, per_agent, sessions, sessions_total, mode, error}
let searchAgent = "*";    // "*" or an agent name
let searchType = "all";   // "all" | "sessions" | "matches"

function searchHash() {
  const q = $("#q").value.trim();
  if (!q) return "search";
  return "search?q=" + encodeURIComponent(q);
}

async function runGlobalSearch() {
  const pattern = $("#q").value.trim();
  if (!pattern) return;
  const fresh = !LAST_SEARCH || LAST_SEARCH.q !== pattern;
  if (fresh) {
    $("#q-result").innerHTML = `<div class="loading"><span class="spin"></span>searching sessions &amp; conversations…</div>`;
    $("#q-sessions").innerHTML = "";
    $("#q-summary").textContent = "";
    try {
      const d = await api("/api/search", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({pattern, agents: ["*"], ignore_case: true,
                              max_results: 300}),
      });
      LAST_SEARCH = Object.assign({}, d, {q: pattern});
    } catch (e) {
      LAST_SEARCH = {q: pattern, error: e.message};
    }
  }
  const hash = searchHash();
  if (location.hash.replace(/^#/, "") !== hash) navigate(hash);
  else switchView("search");
  renderSearch();
}

function renderSearch() {
  const swrap = $("#q-sessions"), wrap = $("#q-result"), sum = $("#q-summary");
  if (!LAST_SEARCH) {
    swrap.innerHTML = ""; sum.textContent = "";
    wrap.innerHTML = `<p class="dim">Nothing searched yet.</p>`;
    return;
  }
  const {q, mode} = LAST_SEARCH;
  if (LAST_SEARCH.error) {
    swrap.innerHTML = ""; sum.textContent = "";
    wrap.innerHTML = `<div class="err">${esc(LAST_SEARCH.error)}</div>`;
    return;
  }
  const ms = (LAST_SEARCH.matches || []).filter(m => searchAgent === "*" || m.agent === searchAgent);
  const ss = (LAST_SEARCH.sessions || []).filter(s => searchAgent === "*" || s.agent === searchAgent);
  const who = searchAgent === "*" ? "all agents" : searchAgent;
  const modeNote = mode === "literal" ? " · not a valid regex, matched literally" : "";
  sum.textContent = `“${q}” · ${who}${modeNote}`;

  // sessions section
  if (searchType !== "matches") {
    if (!ss.length) swrap.innerHTML = "";
    else swrap.innerHTML = `<h2>Sessions <span class="dim small">${ss.length}${LAST_SEARCH.sessions_total > ss.length ? " of " + LAST_SEARCH.sessions_total : ""}</span></h2>`
      + ss.map(s => `
        <div class="match">
          <span class="ref" onclick="openConverse('${esc(s.agent)}','${esc(s.session_id)}')">
            ${esc(s.agent)}/${esc(s.session_id)}</span>
          <span class="ln">${s.name ? esc(s.name) : "&nbsp;"}</span>
          <span class="dim small" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${esc(s.path || "")}">${esc(s.path || "")}</span>
          <span class="dim small" style="margin-left:10px;white-space:nowrap">${s.message_count || 0} msgs</span>
        </div>`).join("");
  } else swrap.innerHTML = "";

  // matches section
  if (searchType !== "sessions") {
    const per = searchAgent === "*" ? (LAST_SEARCH.per_agent || {}) : {};
    const perStr = Object.entries(per).map(([a, n]) => `${esc(a)} <b>${n}</b>`).join(" · ");
    if (!ms.length) {
      wrap.innerHTML = `<p class="dim">No content matches${perStr ? " yet" : ""}.</p>`;
    } else wrap.innerHTML = `<h2>Content matches <span class="dim small">${ms.length} of ${LAST_SEARCH.total}${perStr ? " — " + perStr : ""}</span></h2>`
      + ms.map(m => `
        <div class="match">
          <span class="ref" onclick="openConverse('${esc(m.agent)}','${esc(m.session_id)}')">
            ${esc(m.agent)}/${esc(m.session_id)}</span>
          <span class="ln">${m.line_num}</span>${esc(m.line)}
        </div>`).join("");
  } else wrap.innerHTML = "";
}

async function openConverse(agent, sid) {
  if (CONV && CONV.agent === agent && CONV.session_id === sid) { switchView("converse"); return; }
  const cur = location.hash.replace(/^#/, "");
  if (cur === `session/${agent}/${sid}`) switchView("converse");
  else navigate(`session/${agent}/${sid}`);
  $("#c-title").innerHTML =
    `<a class="crumb" href="#" onclick="openSessions('${esc(agent)}');return false">${esc(agent)}</a>
     <span class="dim"> / </span><span class="crumb-sid" title="${esc(sid)}">${esc(sid)}</span>`;
  $("#c-body").innerHTML = `<div class="loading" style="padding:20px 0"><span class="spin"></span>loading conversation…</div>`;
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
$("#qbtn").addEventListener("click", () => runGlobalSearch());
// logo → overview
$("header h1").addEventListener("click", () => navigate(""));
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") runGlobalSearch(); });
// / or Ctrl/Cmd+K focuses the search bar from anywhere
document.addEventListener("keydown", e => {
  const t = document.activeElement;
  const typing = t && /INPUT|TEXTAREA|SELECT/.test(t.tagName);
  if ((e.key === "/" && !typing) ||
      ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "k")) {
    e.preventDefault();
    $("#q").focus();
    $("#q").select();
  }
});
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
    <div id="fl-body"></div>
    <div class="payloadbox" id="fl-payload"></div>`;
  filterBackend();
}
function filterBackend() {
  const b = $("#fl-backend").value;
  const hint = $("#fl-backend-hint");
  if (b === "decision") {
    hint.textContent = "A local model answers yes/no or picks a label for every concept; passing concepts are kept.";
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
          <select id="fl-endpoint" onchange="filterEndpoint()">
            <option value="systemone">systemone — structured output</option>
            <option value="chat">chat — prompt engineering</option>
          </select>
          <div class="hint" id="fl-endpoint-hint"></div></div>
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
      <div id="fl-thresh-wrap"></div>`;
    filterEndpoint();
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
  // wire live payload preview for all backends
  $("#fl-body").querySelectorAll("input,select,textarea").forEach(el => {
    el.addEventListener("input", updatePayload);
    el.addEventListener("change", updatePayload);
  });
  updatePayload();
}

// Show/hide the threshold field based on the selected endpoint.
// systemone returns probabilities → threshold makes sense.
// chat returns a letter → threshold is meaningless.
function filterEndpoint() {
  const ep = $("#fl-endpoint");
  const wrap = $("#fl-thresh-wrap");
  const hint = $("#fl-endpoint-hint");
  if (!ep || !wrap) return;
  const val = ep.value;
  if (val === "systemone") {
    hint.innerHTML = "Structured output: the model returns typed answers with calibrated " +
      "probabilities — no prompt engineering, nothing to parse. " +
      "The threshold gates the probability.";
    wrap.innerHTML = `
      <div class="field"><label for="fl-thresh">Threshold <span>0–1</span></label>
        <input id="fl-thresh" type="number" step="0.05" min="0" max="1" value="0.6">
        <div class="hint">Keep the concept when the model's confidence is ≥ this value.
          0.6 is a safe start; raise to drop borderline matches, lower to keep more.</div></div>`;
  } else {
    hint.innerHTML = "Prompt engineering: the question is rendered into a lettered prompt " +
      "and the model's text reply is parsed for a letter. No probabilities — only the " +
      "chosen option is available, so there is no threshold to tune.";
    wrap.innerHTML = "";
  }
  // re-wire the threshold field if it appeared
  const t = $("#fl-thresh");
  if (t) {
    t.addEventListener("input", updatePayload);
    t.addEventListener("change", updatePayload);
  }
  // updatePayload is called by filterBackend() after filterEndpoint() returns,
  // but if the user changes the endpoint directly (not via filterBackend),
  // we need to refresh the payload too.
  updatePayload();
}

// Show the exact request a filter would send (or the rules it applies), so the
// user can see what's happening under the hood. Live-updates as the form is
// edited. No network call — the backend just builds the payload.
async function updatePayload() {
  const box = $("#fl-payload");
  if (!box) return;
  const b = $("#fl-backend");
  if (!b) return;
  const backend = b.value;
  const g = id => { const e = $(id); return e ? e.value.trim() : ""; };
  const params = new URLSearchParams();
  params.set("backend", backend);
  if (backend === "decision") {
    params.set("model", g("#fl-model"));
    params.set("instructions", g("#fl-instr"));
    params.set("host", g("#fl-host") || "http://localhost:11434");
    params.set("endpoint", g("#fl-endpoint") || "systemone");
    params.set("mode", g("#fl-mode") || "noul");
    params.set("threshold", g("#fl-thresh") || "0.6");
  } else if (backend === "builtin") {
    params.set("types", g("#fl-types"));
    params.set("exclude", g("#fl-excl"));
    params.set("prompt", g("#fl-prompt"));
  } else {
    params.set("command", g("#fl-cmd"));
    params.set("method", g("#fl-method") || "classify");
  }
  try {
    const d = await api("/api/filter-payload?" + params.toString());
    box.innerHTML = `
      <div class="payload-head">under the hood</div>
      <p class="payload-desc">${esc(d.description)}</p>
      <pre class="payload-pre">${esc(JSON.stringify(d.example, null, 2))}</pre>`;
  } catch (e) {
    box.innerHTML = `<div class="payload-head">under the hood</div>
      <p class="payload-desc dim">${esc(e.message)}</p>`;
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
    const endpoint = g("#fl-endpoint") || "systemone";
    cfg = { type: "systemone", host: g("#fl-host") || "http://localhost:11434",
      model: g("#fl-model"), endpoint: endpoint,
      instructions: g("#fl-instr"), mode: g("#fl-mode") || "noul" };
    // threshold only matters for systemone (probabilities); chat is letter-based
    if (endpoint === "systemone")
      cfg.threshold = parseFloat(g("#fl-thresh") || "0.6");
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
  if (!STRATEGIES.length && !FILTERS.length) {
    // Dropdowns only populate via loadConfigs() on the configs view;
    // make sure they're filled when we jump straight to a conversation.
    const [s, f] = await Promise.all([api("/api/strategies"), api("/api/filters")]);
    STRATEGIES = s.strategies || [];
    FILTERS = f.filters || [];
  }
  const {agent, session_id: sid} = CONV;
  const box = document.createElement("div");
  box.id = "concept-box";
  // Controls
  const stratOpts = `<option value="">none (regex)</option>` +
    STRATEGIES.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  const filterOpts = `<option value="">none</option>` +
    FILTERS.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  // First-time guidance: no strategy and no filter yet → point them at the
  // Concepts page and offer to jump straight to the create form.
  const onboard = (!STRATEGIES.length && !FILTERS.length) ? `
    <div class="onboard">
      <b>Nothing set up yet.</b> Concepts are currently extracted with the
      built-in regex patterns. To extract with an LLM and/or narrow the results,
      create a strategy and/or a filter.
      <a href="#" onclick="navigate('configs?focus=strategy');return false"
         class="onboard-link">Set up your first →</a>
    </div>` : "";
  box.innerHTML = `
    <div class="cp-head">
      <span class="cp-title">Concepts</span>
      <span class="cp-sub">extract &amp; filter</span>
    </div>
    <div class="cp-body">
      ${onboard}
      <div class="toolbar">
        <select id="ex-strat">${stratOpts}</select>
        <select id="ex-filter">${filterOpts}</select>
        <button class="primary" onclick="runExtraction()">Extract</button>
        <label class="small dim" style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="ex-how" checked style="width:auto"> how it works
        </label>
      </div>
      <p class="dim small">Strategy = how to chunk concepts (LLM). Filter = which
        concepts pass through (decision model, built-in, or JSON-RPC).
        <b>How it works</b> shows the pipeline: what goes in, what the model
        says, what the filter drops.</p>
      <div id="ex-results"></div>
    </div>`;
  $("#c-body").prepend(box);
  $("#ex-strat").value = EXTRACTION.strategy || "";
  $("#ex-filter").value = EXTRACTION.filter || "";
  $("#ex-how").addEventListener("change", () => {
    if ($("#ex-how").checked) runExtraction();
    else { const pipe = $("#ex-results .pipe"); if (pipe) pipe.remove(); }
  });
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
  const how = $("#ex-how") ? $("#ex-how").checked : true;
  let qs = "?";
  if (strat) qs += "strategy=" + encodeURIComponent(strat) + "&";
  if (flt) qs += "filter=" + encodeURIComponent(flt) + "&";
  if (how) qs += "explain=1";
  try {
    const d = await api(`/api/agents/${encodeURIComponent(agent)}/sessions/${encodeURIComponent(sid)}/concepts${qs}`);
    let pipe = "";
    if (d.pipeline) pipe = renderPipeline(d.pipeline, d.concepts.length);
    if (!d.concepts.length) {
      res.innerHTML = pipe + `<div class="notice" style="color:var(--dim)">No concepts found.
        ${flt ? "The filter may have dropped everything." : ""}</div>`;
      return;
    }
    res.innerHTML = pipe + d.concepts.map(c => `
      <div class="concept">
        <span class="type">${esc(c.type || "concept")}</span>
        <pre>${esc(c.short || c.medium || c.long || c.description || JSON.stringify(c))}</pre>
      </div>`).join("");
  } catch (e) {
    res.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

// Render the extraction pipeline the backend traced: conversation →
// extraction (LLM or regex) → filter (optional) → concepts.
function renderPipeline(p, finalCount) {
  const kb = n => n >= 1048576 ? (n/1048576).toFixed(1) + " MB" : (n/1024).toFixed(1) + " KB";
  const stages = [];
  stages.push(`<div class="stage">
      <div class="stage-head">1 · conversation</div>
      <div class="stage-body">${p.messages ?? "?"} messages · ${kb(p.chars || 0)} of text</div>
    </div>`);
  if (p.path === "llm") {
    const llm = p.llm || {}, s = p.strategy || {};
    let body = `<b>${esc(s.model || "?")}</b> @ ${esc(s.host || "?")}`;
    if (llm.messages_sent != null) body += ` · sent ${llm.messages_sent} messages (${kb(llm.chars_sent || 0)} incl. prompt)`;
    if (llm.ms != null) body += ` · replied in ${(llm.ms/1000).toFixed(1)}s`;
    let extra = "";
    if (s.prompt) extra += `<details><summary>extraction prompt (sent to the model)</summary><pre class="pipe-pre">${esc(s.prompt)}</pre></details>`;
    if (llm.raw) extra += `<details><summary>raw model reply</summary><pre class="pipe-pre">${esc(llm.raw)}</pre></details>`;
    if (llm.error) extra += `<div class="err">${esc(llm.error)}</div>`;
    stages.push(`<div class="stage">
      <div class="stage-head">2 · extraction — strategy “${esc(s.name || "")}” (LLM)</div>
      <div class="stage-body">${body}</div>${extra}</div>`);
  } else {
    stages.push(`<div class="stage">
      <div class="stage-head">2 · extraction — built-in regex</div>
      <div class="stage-body">scanned every line for <code>Use the following (constraint|goal|preference|observation|reference): …</code></div>
    </div>`);
  }
  let n = 3;
  if (p.filter && typeof p.filter === "object") {
    const f = p.filter;
    let kind;
    if (f.kind === "decision") kind = `decision model <b>${esc(f.model || "?")}</b>: “${esc(f.instructions || "")}”`;
    else {
      kind = "built-in rules";
      if (f.types && f.types.length) kind += ` · types: ${f.types.map(esc).join(", ")}`;
      if (f.prompt) kind += ` · text must contain “${esc(f.prompt)}”`;
    }
    const dropped = (f.dropped || []).length;
    let extra = "";
    if (dropped) extra = `<details><summary>what got dropped (${dropped})</summary><ul class="droplist">` +
      (f.dropped || []).map(d =>
        `<li><b>${esc(d.type || "concept")}</b> ${esc(d.short || "")}
         <span class="dim">— ${esc(d.reason || "")}</span></li>`).join("") +
      "</ul></details>";
    stages.push(`<div class="stage">
      <div class="stage-head">${n} · filter — “${esc(f.name || "")}”</div>
      <div class="stage-body">${kind} · ${f.before ?? "?"} in → <b>${f.after ?? "?"} kept</b>, ${dropped} dropped</div>${extra}</div>`);
    n++;
  }
  stages.push(`<div class="stage">
      <div class="stage-head">${n} · concepts</div>
      <div class="stage-body">${finalCount} shown below</div>
    </div>`);
  return `<div class="pipe">` + stages.join(`<div class="pipe-arrow">▼</div>`) + `</div>`;
}

function hideBootload() {
  const el = $("#bootload");
  if (!el) return;
  el.classList.add("gone");
  setTimeout(() => el.remove(), 350);  // let the fade finish
}
loadOverview()
  .then(applyRoute)
  .then(hideBootload)
  .catch(e => {
    hideBootload();
    const grid = $("#agent-grid");
    if (grid) grid.innerHTML = `<div class="card dim">Could not load agents: ${esc(e.message)}</div>`;
  });
// safety net: never leave the spinner up forever
setTimeout(hideBootload, 15000);
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

def _port_occupant(host: str, port: int) -> str:
    """If something answers on the port, say what it is ('ctools', a server
    banner, or ''). '' means no HTTP answer — the occupant isn't a web server
    we can identify."""
    import urllib.request
    target = host if host in ("0.0.0.0", "::") else "127.0.0.1"
    try:
        with urllib.request.urlopen(f"http://{target}:{port}/api/agents",
                                    timeout=1.5) as r:
            body = r.read(4096).decode(errors="replace")
        if '"version"' in body and '"agents"' in body:
            return "ctools"
        return "a web server"
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser(description="ctools web dashboard")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1 — local only)")
    parser.add_argument("--no-logo", action="store_true",
                        help="skip the animated startup banner")
    args = parser.parse_args()
    url = f"http://{args.host}:{args.port}"

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        if e.errno == 98 or "Address already in use" in str(e):
            who = _port_occupant(args.host, args.port)
            print(f"Port {args.port} is already in use.")
            if who == "ctools":
                print(f"A ctools dashboard is already running here: {url}")
                print("Open that, or stop the other instance first (fuser -k %s/tcp)." % args.port)
            elif who:
                print(f"Something else ({who}) is listening on {args.port}.")
                print(f"Try another one, e.g.:  ctools-web --port {args.port + 1}")
            else:
                print(f"Try another one, e.g.:  ctools-web --port {args.port + 1}")
            raise SystemExit(1)
        raise

    # A random value this process chose at startup. The in-page shutdown button
    # must present it, so a stray request (or a different ctools instance) can't
    # kill this server. It lives in the page HTML only — never in logs.
    import secrets
    Handler._server = server
    Handler._shutdown_token = secrets.token_urlsafe(16)

    if not args.no_logo:
        _render_banner(args.host, args.port, animate=True)
    else:
        print(f"ctools {__version__} dashboard: {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
        server.shutdown()


if __name__ == "__main__":
    main()
