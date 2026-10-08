import json
import os
import stat
import sys
import tempfile
import pytest

from ctools.filterlib import (JSONRPCFilter, SystemOneFilter, load_filter,
                              extract_decision_letter)


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


class TestSystemOneFilter:
    """SystemOneFilter talks to Ollama /v1/systemone. requests is mocked."""

    def _mock_response(self, answers):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"model": "m", "answers": answers}
        return R()

    def test_noul_above_threshold_passes(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_response(
            {"relevant": {"type": "noul", "noul": 0.9}}))
        f = SystemOneFilter(model="m", instructions="is it relevant?")
        assert f.filter("text") is True

    def test_noul_below_threshold_filters(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_response(
            {"relevant": {"type": "noul", "noul": 0.2}}))
        f = SystemOneFilter(model="m", instructions="is it relevant?", threshold=0.5)
        assert f.filter("text") is False

    def test_noul_default_threshold(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_response(
            {"relevant": {"type": "noul", "noul": 0.599}}))
        f = SystemOneFilter(model="m", instructions="q?")
        # default threshold 0.6 -> 0.599 filters
        assert f.filter("text") is False

    def test_choice_in_allowed_passes(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_response(
            {"category": {"type": "choice", "choice": "coding",
                          "probabilities": {"coding": 0.9, "security": 0.1},
                          "confidence": 0.85}}))
        f = SystemOneFilter(model="m", instructions="which category?",
                            question_name="category",
                            mode="choice", allowed=["coding", "process"],
                            criteria={"coding": "code", "security": "secrets",
                                      "process": "workflow"})
        assert f.filter("text") is True

    def test_choice_not_in_allowed_filters(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_response(
            {"category": {"type": "choice", "choice": "security"}}))
        f = SystemOneFilter(model="m", instructions="which category?",
                            question_name="category",
                            mode="choice", allowed=["coding"],
                            criteria={"coding": "code", "security": "secrets"})
        assert f.filter("text") is False

    def test_choice_no_allowlist_passes(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_response(
            {"relevant": {"type": "choice", "choice": "anything"}}))
        f = SystemOneFilter(model="m", instructions="which category?", mode="choice")
        assert f.filter("text") is True

    def test_error_defaults_to_true(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise ConnectionError("no server")
        monkeypatch.setattr(requests, "post", boom)
        f = SystemOneFilter(model="m", instructions="q?")
        assert f.filter("text") is True

    def test_http_error_defaults_to_true(self, monkeypatch):
        import requests

        class R:
            def raise_for_status(self):
                raise requests.HTTPError(404)
        monkeypatch.setattr(requests, "post", lambda *a, **k: R())
        f = SystemOneFilter(model="m", instructions="q?")
        assert f.filter("text") is True

    def test_missing_answer_defaults_to_true(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_response({}))
        f = SystemOneFilter(model="m", instructions="q?")
        assert f.filter("text") is True

    def test_missing_model_raises(self):
        with pytest.raises(ValueError):
            SystemOneFilter(model="", instructions="q?")

    def test_missing_instructions_raises(self):
        with pytest.raises(ValueError):
            SystemOneFilter(model="m", instructions="")

    def test_sends_state_and_question(self, monkeypatch):
        import requests
        captured = {}

        def capture(url, json=None, timeout=None, headers=None):
            captured["url"] = url
            captured["payload"] = json
            return self._mock_response({"relevant": {"type": "noul", "noul": 0.9}})
        monkeypatch.setattr(requests, "post", capture)
        f = SystemOneFilter(host="http://10.0.0.1:11434/", model="tev1",
                            instructions="is it crypto?")
        f.filter("use AES-256")
        assert captured["url"] == "http://10.0.0.1:11434/v1/systemone"
        assert captured["payload"]["model"] == "tev1"
        assert captured["payload"]["state"] == "use AES-256"
        assert captured["payload"]["questions"]["relevant"]["type"] == "noul"

    def test_repr(self):
        f = SystemOneFilter(model="m", instructions="q?")
        assert "SystemOneFilter" in repr(f)
        assert "endpoint='systemone'" in repr(f)


class TestExtractDecisionLetter:
    def test_bare_letter(self):
        assert extract_decision_letter("A") == "A"
        assert extract_decision_letter("b") == "b"

    def test_letter_with_punctuation(self):
        assert extract_decision_letter("A.") == "A"
        assert extract_decision_letter("(A)") == "A"

    def test_wrapped_letter(self):
        assert extract_decision_letter("The answer is A") == "A"
        assert extract_decision_letter("A) yes, it is") == "A"

    def test_glued_falls_back_to_first(self):
        # No standalone letter -> best effort: first letter.
        assert extract_decision_letter("answerA") == "a"

    def test_empty(self):
        assert extract_decision_letter("") == ""


class TestSystemOneChatEndpoint:
    """The portable /v1/chat/completions endpoint, requests mocked."""

    def _mock_chat(self, content, reasoning=""):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                msg = {"role": "assistant", "content": content}
                if reasoning:
                    msg["reasoning"] = reasoning
                return {"choices": [{"message": msg}]}
        return R()

    def test_chat_noul_letter_a_passes(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_chat("A"))
        f = SystemOneFilter(model="m", instructions="q?", endpoint="chat")
        assert f.filter("text") is True

    def test_chat_noul_letter_b_filters(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_chat("B"))
        f = SystemOneFilter(model="m", instructions="q?", endpoint="chat")
        assert f.filter("text") is False

    def test_chat_wrapped_answer(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k:
                            self._mock_chat("The answer is B"))
        f = SystemOneFilter(model="m", instructions="q?", endpoint="chat")
        assert f.filter("text") is False

    def test_chat_ignores_reasoning(self, monkeypatch):
        # A thinking model's deliberation may start with a letter; only the
        # final `content` decides.
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k:
                            self._mock_chat("A", reasoning="The user wants..."))
        f = SystemOneFilter(model="m", instructions="q?", endpoint="chat")
        assert f.filter("text") is True

    def test_chat_empty_content_passes(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k:
                            self._mock_chat("", reasoning="still thinking"))
        f = SystemOneFilter(model="m", instructions="q?", endpoint="chat")
        assert f.filter("text") is True

    def test_chat_choice_letter_maps_to_key(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_chat("B"))
        f = SystemOneFilter(model="m", instructions="category?", endpoint="chat",
                            mode="choice", allowed=["coding"],
                            criteria={"coding": "code", "security": "secrets"})
        # B -> security, not allowed -> filter
        assert f.filter("text") is False

    def test_chat_choice_allowed_passes(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: self._mock_chat("A"))
        f = SystemOneFilter(model="m", instructions="category?", endpoint="chat",
                            mode="choice", allowed=["coding"],
                            criteria={"coding": "code", "security": "secrets"})
        assert f.filter("text") is True

    def test_chat_url_and_payload(self, monkeypatch):
        import requests
        captured = {}

        def capture(url, json=None, timeout=None, headers=None):
            captured["url"] = url
            captured["payload"] = json
            return self._mock_chat("A")
        monkeypatch.setattr(requests, "post", capture)
        f = SystemOneFilter(host="http://h:1", model="laya", instructions="q?",
                            endpoint="chat", max_tokens=512, api_key="k")
        f.filter("STATE")
        assert captured["url"] == "http://h:1/v1/chat/completions"
        assert captured["payload"]["model"] == "laya"
        assert captured["payload"]["max_tokens"] == 512
        prompt = captured["payload"]["messages"][0]["content"]
        assert "STATE" in prompt
        assert "Question: q?" in prompt
        assert "Return only the letter" in prompt

    def test_chat_prompt_uses_criteria(self, monkeypatch):
        import requests
        captured = {}
        monkeypatch.setattr(requests, "post", lambda *a, **k:
                            (captured.update(json=k.get("json")), self._mock_chat("A"))[-1])
        f = SystemOneFilter(model="m", instructions="category?", endpoint="chat",
                            mode="choice",
                            criteria={"coding": "code rules", "security": "secrets"})
        f.filter("text")
        prompt = captured["json"]["messages"][0]["content"]
        assert "A) coding (code rules)" in prompt
        assert "B) security (secrets)" in prompt

    def test_chat_error_defaults_to_true(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise ConnectionError("no server")
        monkeypatch.setattr(requests, "post", boom)
        f = SystemOneFilter(model="m", instructions="q?", endpoint="chat")
        assert f.filter("text") is True

    def test_invalid_endpoint_raises(self):
        with pytest.raises(ValueError, match="endpoint"):
            SystemOneFilter(model="m", instructions="q?", endpoint="nope")

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="mode"):
            SystemOneFilter(model="m", instructions="q?", mode="nonsense")


class TestLoadFilter:
    def test_load_systemone_from_dict(self):
        f = load_filter({"type": "systemone", "model": "tev1",
                         "instructions": "is it relevant?"})
        assert isinstance(f, SystemOneFilter)
        assert f.model == "tev1"

    def test_load_systemone_by_shape(self):
        # A model without a command is a decision-model filter.
        f = load_filter({"model": "nimble", "instructions": "q?"})
        assert isinstance(f, SystemOneFilter)
        assert f.model == "nimble"

    def test_load_jsonrpc_explicit(self):
        f = load_filter({"type": "jsonrpc", "command": ["echo"], "model": "x",
                         "instructions": "q?"})
        assert isinstance(f, JSONRPCFilter)

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

    def test_load_systemone_from_file(self, tmp_path):
        config_path = tmp_path / "filter.json"
        config_path.write_text(json.dumps({"type": "systemone", "model": "clef-flash",
                                           "instructions": "is it relevant?"}))
        f = load_filter(str(config_path))
        assert isinstance(f, SystemOneFilter)
        assert f.model == "clef-flash"

    def test_load_systemone_endpoint_chat(self):
        f = load_filter({"type": "systemone", "model": "laya",
                         "endpoint": "chat", "host": "http://vllm:8000",
                         "api_key": "secret", "instructions": "q?"})
        assert isinstance(f, SystemOneFilter)
        assert f.endpoint == "chat"
        assert f.host == "http://vllm:8000"
        assert f.api_key == "secret"
