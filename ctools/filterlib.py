#!/usr/bin/env python3
"""
filterlib - Binary classifier filters.

filter(in_str) -> True to pass through, False to filter out.
Default on error is True (pass through).

Two backends:

  * JSONRPCFilter    - a subprocess that speaks JSON-RPC 2.0 over stdio.
  * SystemOneFilter  - a local *decision model*, asked over either Ollama's
                       typed /v1/systemone endpoint (probabilities) or any
                       OpenAI-compatible /v1/chat/completions endpoint
                       (lettered decision task), selected by ``endpoint``.

The JSON-RPC contract:
  stdin:  {"jsonrpc":"2.0","id":1,"method":"classify","params":{"content":"..."}}
  stdout: {"jsonrpc":"2.0","id":1,"result":true}

The decision-model contract (SystemOneFilter):
  endpoint "systemone":
    POST {host}/v1/systemone  {"model":..., "state": <content>, "questions": {...}}
    -> {"answers": {"<q>": {"type": "noul", "noul": 0.93}, ...}}
  endpoint "chat":
    POST {host}/v1/chat/completions  {"model":..., "messages": [...], ...}
    -> the model's lettered decision-task reply, parsed for the letter.

A decision model is a small, fast classifier that scores a typed question
against some state and returns calibrated probabilities (no free text to
parse). It makes for a cheap, local relevance/policy filter: ask "is this
concept about X?" and pass it through only when the model is confident
enough. It can be served by Ollama's native /v1/systemone endpoint (typed
questions, probabilities) or by any OpenAI-compatible /v1/chat/completions
endpoint (lettered decision-task prompt), selected by the ``endpoint`` field.

Usage:
    from ctools.filterlib import JSONRPCFilter, SystemOneFilter, load_filter

    f = JSONRPCFilter("./my_filter.py")
    f.filter("password: secret123")  # calls subprocess

    d = SystemOneFilter(host="http://localhost:11434", model="tev1",
                        instructions="Is this about cryptography?")
    d.filter("we must use AES-256, never MD5")  # True, if confident enough
"""

import json
import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ctools.log import get_logger

try:
    import requests
except ImportError:  # graceful degradation: filter defaults to pass-through
    requests = None

from pathlib import Path as _Path
FILTERS_DIR = _Path.home() / ".config" / "ctools" / "filters"


class Filter(ABC):
    """Base class for binary classifiers.

    filter(in_str) returns True to pass through, False to filter out.
    Default on error is True (pass through).
    """

    @abstractmethod
    def filter(self, in_str: str) -> bool:
        pass


class JSONRPCFilter(Filter):
    """Subprocess JSON-RPC 2.0 filter.

    Spawns a subprocess and sends a JSON-RPC request on stdin.
    Expects a JSON-RPC response on stdout with result: true/false.
    Defaults to True on any error.
    """

    def __init__(self, command: Union[str, List[str]], method: str = "classify",
                 timeout: float = 30.0):
        self.command = command
        self.method = method
        self.timeout = timeout

    def filter(self, in_str: str) -> bool:
        log = get_logger()
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": self.method,
            "params": {"content": in_str},
        }
        try:
            cmd = self.command if isinstance(self.command, list) else [self.command]
            log.debug("filter_call", command=cmd, method=self.method, input_len=len(in_str))
            proc = subprocess.run(
                cmd,
                input=json.dumps(request) + "\n",
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

            if proc.returncode != 0:
                log.warning("filter_nonzero_exit", command=cmd, returncode=proc.returncode, stderr=proc.stderr[:200] if proc.stderr else None)
                return True

            response = json.loads(proc.stdout.strip())
            result = bool(response.get("result", True))
            log.debug("filter_result", result=result, command=cmd)
            return result

        except subprocess.TimeoutExpired:
            log.warning("filter_timeout", command=self.command, timeout=self.timeout)
            return True
        except json.JSONDecodeError as e:
            log.warning("filter_bad_json", command=self.command, error=str(e))
            return True
        except OSError as e:
            log.warning("filter_os_error", command=self.command, error=str(e))
            return True

    def __repr__(self) -> str:
        return f"JSONRPCFilter(command={self.command!r}, method={self.method!r})"


def extract_decision_letter(content: str) -> str:
    """Extract a decision letter from a model's chat reply.

    Decision models are tuned to answer with a single letter, but a server
    (or a thinking model that leaks) can wrap it. Resolution order:

    1. The reply is exactly one letter (case-insensitive) -> that letter.
    2. The first *standalone* letter (not glued to other letters) -> that
       letter, e.g. ``A``, ``A.`` or ``(A)`` in prose.
    3. Otherwise -> the first letter of the reply (best effort).

    Returns "" when there is no letter at all.
    """
    text = content.strip()
    if not text:
        return ""
    if len(text) == 1 and text.isalpha():
        return text
    m = re.search(r"(?<![A-Za-z])([A-Za-z])(?![A-Za-z])", text)
    if m:
        return m.group(1)
    m = re.search(r"[A-Za-z]", text)
    return m.group(0) if m else ""


class SystemOneFilter(Filter):
    """Decision-model filter: a local classifier that scores each input.

    Asks a decision model a typed question about each input and decides
    pass/filter from the answer.

    Two endpoints (``endpoint``):

    * ``"systemone"`` (default): Ollama's native typed endpoint
      (``POST /v1/systemone``). The request carries the state and typed
      questions; the response carries a calibrated probability per option.
      No free text is generated, so there is nothing to parse. Requires a
      server that exposes the typed endpoint (e.g. Ollama 0.40.x).
    * ``"chat"``: the portable OpenAI-compatible endpoint
      (``POST /v1/chat/completions``). Works on any server that can run the
      model (vLLM, older Ollama, any OpenAI-compatible gateway). The typed
      question is rendered into the model's lettered decision-task prompt and
      the model's lettered reply is parsed. No probabilities, so only the
      chosen answer is available.

    Two modes (``mode``):

    * ``"noul"`` (default): a yes/no question. systemone: pass through when
      the probability is >= ``threshold``. chat: pass through when the model
      picks the yes option.
    * ``"choice"``: a multi-way question. Pass through when the model's
      chosen category is in ``allowed`` (the criteria keys to accept).

    On any error (network, missing model, unexpected response) the filter
    defaults to ``True`` (pass through), matching the rest of filterlib.
    """

    ENDPOINTS = ("systemone", "chat")

    def __init__(self, host: str = "http://localhost:11434",
                 model: str = "", instructions: str = "",
                 endpoint: str = "systemone",
                 question_name: str = "relevant",
                 mode: str = "noul", threshold: float = 0.6,
                 allowed: Optional[List[str]] = None,
                 criteria: Optional[Dict[str, str]] = None,
                 max_tokens: int = 2048, api_key: Optional[str] = None,
                 timeout: float = 30.0):
        if not model:
            raise ValueError("SystemOneFilter requires a model")
        if not instructions:
            raise ValueError("SystemOneFilter requires instructions")
        if endpoint not in self.ENDPOINTS:
            raise ValueError(f"endpoint must be one of {self.ENDPOINTS}, got {endpoint!r}")
        if mode not in ("noul", "choice"):
            raise ValueError(f"mode must be 'noul' or 'choice', got {mode!r}")
        self.host = host.rstrip("/")
        self.model = model
        self.instructions = instructions
        self.endpoint = endpoint
        self.question_name = question_name
        self.mode = mode
        self.threshold = threshold
        self.allowed = allowed or []
        self.criteria = criteria or {}
        self.max_tokens = max_tokens
        self.api_key = api_key
        self.timeout = timeout

    # --- request building ---

    def _questions(self) -> Dict[str, Any]:
        if self.mode == "choice":
            criteria = self.criteria or {k: k for k in self.allowed}
            return {self.question_name: {
                "type": "choice",
                "instructions": self.instructions,
                "criteria": criteria,
            }}
        # noul mode
        question: Dict[str, Any] = {
            "type": "noul",
            "instructions": self.instructions,
        }
        if self.criteria:
            question["criteria"] = self.criteria
        return {self.question_name: question}

    def _chat_prompt(self, state: str) -> str:
        """Render the question as a lettered decision task for the chat endpoint."""
        lines = ["Decision task:", "", "state:", "```", state, "```", "",
                 f"Question: {self.instructions}", "Options:"]
        if self.mode == "choice":
            options = self.criteria or {k: k for k in self.allowed}
            for i, (key, desc) in enumerate(options.items()):
                letter = chr(ord("A") + i)
                text = f"{letter}) {key}"
                if desc and desc != key:
                    text += f" ({desc})"
                lines.append(text)
            if self.allowed:
                lines.append("")
                lines.append(f"Accept only these options: {', '.join(self.allowed)}.")
        else:
            options = self.criteria
            if options:
                for i, (key, desc) in enumerate(options.items()):
                    letter = chr(ord("A") + i)
                    text = f"{letter}) {key}"
                    if desc and desc != key:
                        text += f" ({desc})"
                    lines.append(text)
            else:
                lines.append("A) Yes.")
                lines.append("B) No.")
        lines += ["", "Return only the letter of the selected option, with no explanation."]
        return "\n".join(lines)

    # --- decisions ---

    def _decide_systemone(self, answer: Dict[str, Any]) -> bool:
        if self.mode == "choice":
            chosen = answer.get("choice")
            if chosen is None:
                return True
            if not self.allowed:
                return True
            return chosen in self.allowed
        prob = answer.get("noul")
        if prob is None:
            return True
        return prob >= self.threshold

    def _decide_chat(self, letter: str) -> bool:
        if not letter:
            return True
        letter = letter.upper()
        if self.mode == "choice":
            options = self.criteria or {k: k for k in self.allowed}
            index = ord(letter) - ord("A")
            if index < 0 or index >= len(options):
                return True
            key = list(options)[index]
            if not self.allowed:
                return True
            return key in self.allowed
        # noul: A (or "yes") = pass, B (or "no") = filter
        if letter in ("B", "NO"):
            return False
        return True

    # --- HTTP ---

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _call_systemone(self, in_str: str, log) -> bool:
        payload = {"model": self.model, "state": in_str,
                   "questions": self._questions()}
        r = requests.post(f"{self.host}/v1/systemone", json=payload,
                          headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        answers = r.json().get("answers", {})
        answer = answers.get(self.question_name)
        if not isinstance(answer, dict):
            log.warning("systemone_missing_answer", model=self.model,
                        question=self.question_name)
            return True
        result = self._decide_systemone(answer)
        log.debug("systemone_result", model=self.model, question=self.question_name,
                  mode=self.mode, result=result,
                  noul=answer.get("noul"), choice=answer.get("choice"))
        return result

    def _call_chat(self, in_str: str, log) -> bool:
        payload = {
            "model": self.model,
            "messages": [{"role": "user",
                          "content": self._chat_prompt(in_str)}],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        r = requests.post(f"{self.host}/v1/chat/completions", json=payload,
                          headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        message = r.json()["choices"][0]["message"]
        # For thinking models the deliberation lands in `reasoning` and the
        # final answer in `content`. The decision models are tuned to emit just
        # the letter, so the letter in `content` is the decision. If `content`
        # has no letter (it was not generated), pass through.
        content = (message.get("content") or "").strip()
        letter = extract_decision_letter(content)
        result = self._decide_chat(letter)
        log.debug("systemone_result", model=self.model, question=self.question_name,
                  endpoint="chat", mode=self.mode, result=result,
                  letter=letter or None, content=content[:80] if content else None)
        return result

    def filter(self, in_str: str) -> bool:
        log = get_logger()
        if requests is None:
            log.warning("systemone_no_requests", error="requests not installed")
            return True
        try:
            if self.endpoint == "systemone":
                return self._call_systemone(in_str, log)
            return self._call_chat(in_str, log)
        except Exception as e:
            log.warning("systemone_error", model=self.model, host=self.host,
                        endpoint=self.endpoint, error=str(e))
            return True

    def __repr__(self) -> str:
        return (f"SystemOneFilter(model={self.model!r}, endpoint={self.endpoint!r}, "
                f"mode={self.mode!r}, threshold={self.threshold!r}, "
                f"allowed={self.allowed!r})")


def _is_systemone_config(config: Dict[str, Any]) -> bool:
    """True when the config describes a decision-model filter."""
    if config.get("type") == "systemone":
        return True
    if config.get("type") == "jsonrpc":
        return False
    # No explicit type: a decision-model config names a model with no command.
    return bool(config.get("model")) and not config.get("command")


def load_filter(config: Union[str, Path, Dict[str, Any]]) -> Filter:
    """Load a filter from a JSON file path or config dict.

    Dispatches on the config shape:

    * decision model: ``{"type": "systemone", "host": ..., "model": ..., "instructions": ...}``
      -> ``SystemOneFilter``
    * subprocess:     ``{"command": ..., "method": ..., "timeout": ...}``
      -> ``JSONRPCFilter``

    A config with a ``model`` and no ``command`` is treated as a decision-model
    filter even without an explicit ``type``.
    """
    if isinstance(config, (str, Path)):
        p = Path(config)
        if not p.exists():
            raise FileNotFoundError(f"Filter config not found: {config}")
        with open(p) as f:
            config = json.load(f)

    if not isinstance(config, dict):
        raise ValueError(f"Filter config must be a dict, got {type(config).__name__}")

    if _is_systemone_config(config):
        f = SystemOneFilter(
            host=config.get("host", "http://localhost:11434"),
            model=config["model"],
            instructions=config["instructions"],
            endpoint=config.get("endpoint", "systemone"),
            question_name=config.get("question_name", "relevant"),
            mode=config.get("mode", "noul"),
            threshold=config.get("threshold", 0.6),
            allowed=config.get("allowed"),
            criteria=config.get("criteria"),
            max_tokens=config.get("max_tokens", 2048),
            api_key=config.get("api_key"),
            timeout=config.get("timeout", 30.0),
        )
        get_logger().debug("filter_loaded", kind="systemone", model=f.model,
                           endpoint=f.endpoint, mode=f.mode,
                           threshold=f.threshold, allowed=f.allowed)
        return f

    command = config.get("command", [])
    method = config.get("method", "classify")
    timeout = config.get("timeout", 30.0)
    get_logger().debug("filter_loaded", kind="jsonrpc", command=command,
                       method=method, timeout=timeout)
    return JSONRPCFilter(command, method=method, timeout=timeout)
