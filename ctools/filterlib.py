#!/usr/bin/env python3
"""
filterlib - Binary classifier filters via JSON-RPC 2.0.

filter(in_str) -> True to pass through, False to filter out.
Default on error is True (pass through).

Filter scripts speak JSON-RPC 2.0 over stdio:
  stdin:  {"jsonrpc":"2.0","id":1,"method":"classify","params":{"content":"..."}}
  stdout: {"jsonrpc":"2.0","id":1,"result":true}

Usage:
    from ctools.filterlib import JSONRPCFilter

    f = JSONRPCFilter("./my_filter.py")
    f.filter("password: secret123")  # calls subprocess
"""

import json
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Union

from ctools.log import get_logger


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


def load_filter(config: Union[str, Path, Dict[str, Any]]) -> Filter:
    """Load a filter from a JSON file path or config dict.

    Config schema:
        {"command": "path_or_cmd", "method": "classify", "timeout": 30}
    """
    if isinstance(config, (str, Path)):
        p = Path(config)
        if not p.exists():
            raise FileNotFoundError(f"Filter config not found: {config}")
        with open(p) as f:
            config = json.load(f)

    if not isinstance(config, dict):
        raise ValueError(f"Filter config must be a dict, got {type(config).__name__}")

    command = config.get("command", [])
    method = config.get("method", "classify")
    timeout = config.get("timeout", 30.0)
    get_logger().debug("filter_loaded", command=command, method=method, timeout=timeout)
    return JSONRPCFilter(command, method=method, timeout=timeout)
