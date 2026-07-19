import json
import os
import stat
import sys
import tempfile
import pytest
from pathlib import Path

from ctools.filterlib import Filter, JSONRPCFilter, load_filter


def _make_executable(script_content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(script_content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


ALLOW_SCRIPT = '''#!/usr/bin/env python3
import json, sys
req = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": True}))
'''

DENY_SCRIPT = '''#!/usr/bin/env python3
import json, sys
req = json.loads(sys.stdin.readline())
content = req["params"]["content"].lower()
has_password = "password" in content
print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": not has_password}))
'''


class TestJSONRPCFilter:
    def test_allow_passes_through(self):
        script = _make_executable(ALLOW_SCRIPT)
        try:
            f = JSONRPCFilter(script)
            assert f.filter("any text") is True
        finally:
            os.unlink(script)

    def test_deny_filters_out(self):
        script = _make_executable(DENY_SCRIPT)
        try:
            f = JSONRPCFilter(script)
            assert f.filter("my password is x") is False
            assert f.filter("hello world") is True
        finally:
            os.unlink(script)

    def test_list_command(self):
        script = _make_executable(ALLOW_SCRIPT)
        try:
            f = JSONRPCFilter([sys.executable, script])
            assert f.filter("anything") is True
        finally:
            os.unlink(script)

    def test_error_defaults_to_true(self):
        f = JSONRPCFilter("/nonexistent/command")
        assert f.filter("anything") is True

    def test_bad_json_defaults_to_true(self):
        script = _make_executable('#!/usr/bin/env python3\nprint("not json")')
        try:
            f = JSONRPCFilter(script)
            assert f.filter("anything") is True
        finally:
            os.unlink(script)

    def test_timeout_defaults_to_true(self):
        script = _make_executable('#!/usr/bin/env python3\nimport time\ntime.sleep(100)')
        try:
            f = JSONRPCFilter(script, timeout=0.1)
            assert f.filter("anything") is True
        finally:
            os.unlink(script)

    def test_nonzero_exit_defaults_to_true(self):
        script = _make_executable('#!/usr/bin/env python3\nimport sys; sys.exit(1)')
        try:
            f = JSONRPCFilter(script)
            assert f.filter("anything") is True
        finally:
            os.unlink(script)

    def test_custom_method(self):
        script = _make_executable('''#!/usr/bin/env python3
import json, sys
req = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": True}))
''')
        try:
            f = JSONRPCFilter(script, method="custom_method")
            assert f.filter("anything") is True
        finally:
            os.unlink(script)

    def test_repr(self):
        f = JSONRPCFilter("my_filter.py")
        assert "JSONRPCFilter" in repr(f)
        assert "my_filter.py" in repr(f)

    def test_repr_list(self):
        f = JSONRPCFilter([sys.executable, "script.py"])
        assert "script.py" in repr(f)


class TestLoadFilter:
    def test_load_from_dict(self):
        script = _make_executable(ALLOW_SCRIPT)
        try:
            f = load_filter({"command": script})
            assert isinstance(f, JSONRPCFilter)
            assert f.filter("anything") is True
        finally:
            os.unlink(script)

    def test_load_from_file(self, tmp_path):
        script = _make_executable(ALLOW_SCRIPT)
        config_path = tmp_path / "filter.json"
        try:
            config_path.write_text(json.dumps({"command": script}))
            f = load_filter(str(config_path))
            assert isinstance(f, JSONRPCFilter)
            assert f.filter("anything") is True
        finally:
            os.unlink(script)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_filter("/nonexistent/filter.json")

    def test_not_a_dict(self):
        with pytest.raises(ValueError, match="must be a dict"):
            load_filter(123)
