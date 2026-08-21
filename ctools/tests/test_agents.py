"""Tests for the agent layer itself: the interface every command relies on."""

import json
from datetime import datetime

import pytest

from ctools.agents import (
    AGENT_CLASSES, REGISTRY, Agent, ClaudeCodeAgent, ClaudeDesktopAgent,
    CodexAgent, GooseAgent, OpencodeAgent, PiAgent, SessionNotFound,
    UnsupportedOperation, epoch_ms, file_metadata, parse_timestamp, text_of,
)


# --- Registry contract ---

def test_registry_keys_match_class_names():
    for name, agent in REGISTRY.items():
        assert agent.name == name
        assert isinstance(agent, Agent)


def test_registry_covers_every_agent_class():
    assert {cls.name for cls in AGENT_CLASSES} == set(REGISTRY)


def test_every_agent_implements_the_read_interface():
    """sessions() and messages() are the two methods with no default."""
    for cls in AGENT_CLASSES:
        assert cls.sessions is not Agent.sessions, f"{cls.__name__} cannot list sessions"
        assert cls.messages is not Agent.messages or cls.raw_messages is not Agent.raw_messages, \
            f"{cls.__name__} cannot read messages"


def test_every_agent_declares_a_default_path():
    for cls in AGENT_CLASSES:
        assert cls.default_base_path().is_absolute()


def test_agent_takes_an_explicit_base_path(tmp_path):
    agent = OpencodeAgent(tmp_path)
    assert agent.base_path == tmp_path
    assert agent.db_path == tmp_path / 'opencode.db'


def test_source_points_at_what_is_actually_read(tmp_path):
    assert ClaudeCodeAgent(tmp_path).source == tmp_path / 'projects/'
    assert OpencodeAgent(tmp_path).source == tmp_path / 'opencode.db'


def test_label_prefers_display_name():
    assert REGISTRY['claude-code'].label == 'Claude Code'


# --- text_of ---

def test_text_of_plain_string():
    assert text_of("hello") == "hello"


def test_text_of_block_list():
    assert text_of([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"


def test_text_of_skips_non_text_blocks():
    blocks = [{"type": "text", "text": "keep"},
              {"type": "tool_use", "id": "t1"},
              {"type": "thinking", "thinking": "drop"}]
    assert text_of(blocks) == "keep"


def test_text_of_codex_blocks():
    assert text_of([{"type": "input_text", "text": "in"},
                    {"type": "output_text", "text": "out"}]) == "in\nout"


def test_text_of_goose_legacy_blocks():
    assert text_of([{"Text": {"text": "legacy"}}]) == "legacy"


def test_text_of_single_block():
    assert text_of({"type": "text", "text": "solo"}) == "solo"


def test_text_of_nonsense():
    assert text_of(None) == ""
    assert text_of(42) == ""
    assert text_of([None, 7]) == ""


# --- Timestamps ---

def test_parse_timestamp_with_zulu():
    assert parse_timestamp("2026-01-02T03:04:05Z") is not None


def test_parse_timestamp_with_nanoseconds():
    """Goose writes nanosecond precision, which fromisoformat rejects."""
    assert parse_timestamp("2026-01-02T03:04:05.123456789Z") is not None


def test_parse_timestamp_returns_naive_local():
    assert parse_timestamp("2026-01-02T03:04:05Z").tzinfo is None


def test_parse_timestamp_rejects_junk():
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None


def test_epoch_ms():
    assert epoch_ms(1700000000000) == datetime.fromtimestamp(1700000000)
    assert epoch_ms(None) is None
    assert epoch_ms(0) is None


def test_file_metadata(tmp_path):
    path = tmp_path / 'f.txt'
    path.write_text('hello')
    ctime, mtime, size = file_metadata(path)
    assert size == 5
    assert isinstance(ctime, datetime) and isinstance(mtime, datetime)


# --- FileAgent lookup ---

def test_session_file_matches_on_stem(tmp_path):
    (tmp_path / 'projects').mkdir()
    target = tmp_path / 'projects' / 'ses_a.jsonl'
    target.write_text('')
    assert ClaudeCodeAgent(tmp_path).session_file('ses_a') == target


def test_missing_session_file_raises(tmp_path):
    with pytest.raises(SessionNotFound):
        ClaudeCodeAgent(tmp_path).messages('nope')


def test_session_not_found_names_agent_and_session(tmp_path):
    with pytest.raises(SessionNotFound) as excinfo:
        ClaudeCodeAgent(tmp_path).messages('ses_x')
    assert excinfo.value.agent == 'claude-code'
    assert excinfo.value.session_id == 'ses_x'


# --- JsonlAgent reading ---

def _claude_code_session(tmp_path, entries, session_id='ses_a'):
    project = tmp_path / 'projects' / 'proj'
    project.mkdir(parents=True)
    path = project / f'{session_id}.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
    return path


def test_claude_code_reads_current_entry_types(tmp_path):
    """Current Claude Code writes type 'user'/'assistant' with block content."""
    _claude_code_session(tmp_path, [
        {"type": "mode", "mode": "normal"},
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "hi"}]}},
    ])
    messages = ClaudeCodeAgent(tmp_path).messages('ses_a')
    assert [(m.role, m.content) for m in messages] == [('user', 'hello'), ('assistant', 'hi')]


def test_claude_code_reads_legacy_human_entries(tmp_path):
    _claude_code_session(tmp_path, [
        {"type": "human", "message": {"content": "old style"}},
    ])
    messages = ClaudeCodeAgent(tmp_path).messages('ses_a')
    assert [(m.role, m.content) for m in messages] == [('user', 'old style')]


def test_claude_code_session_name_from_ai_title(tmp_path):
    _claude_code_session(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "first prompt"}},
        {"type": "ai-title", "aiTitle": "A Good Title"},
    ])
    assert ClaudeCodeAgent(tmp_path).sessions()[0].name == 'A Good Title'


def test_claude_code_session_name_falls_back_to_first_prompt(tmp_path):
    _claude_code_session(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "first prompt"}},
    ])
    assert ClaudeCodeAgent(tmp_path).sessions()[0].name == 'first prompt'


def test_claude_code_records_model(tmp_path):
    _claude_code_session(tmp_path, [
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5",
                                          "content": [{"type": "text", "text": "hi"}]}},
    ])
    assert ClaudeCodeAgent(tmp_path).sessions()[0].model == 'claude-opus-5'


def test_lines_are_derived_from_messages(tmp_path):
    _claude_code_session(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "one\n\ntwo"}},
    ])
    assert ClaudeCodeAgent(tmp_path).lines('ses_a') == [
        (1, 'user: one'), (2, 'user: two')]


def test_raw_messages_keeps_system_turns(tmp_path):
    _claude_code_session(tmp_path, [
        {"type": "system", "message": {"role": "system", "content": "be brief"}},
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])
    agent = ClaudeCodeAgent(tmp_path)
    assert [m.role for m in agent.raw_messages('ses_a')] == ['system', 'user']
    assert [m.role for m in agent.messages('ses_a')] == ['user']


# --- JsonlAgent writing ---

def test_inject_system_prepends_when_absent(tmp_path):
    path = _claude_code_session(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])
    ClaudeCodeAgent(tmp_path).inject_system('ses_a', 'be brief')
    first = json.loads(path.read_text().splitlines()[0])
    assert first['message']['content'] == 'be brief'


def test_inject_system_replaces_when_present(tmp_path):
    path = _claude_code_session(tmp_path, [
        {"type": "system", "message": {"role": "system", "content": "old"}},
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])
    ClaudeCodeAgent(tmp_path).inject_system('ses_a', 'new')
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])['message']['content'] == 'new'


def test_inject_toolcall_appends_and_then_replaces(tmp_path):
    path = _claude_code_session(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])
    agent = ClaudeCodeAgent(tmp_path)
    agent.inject_toolcall('ses_a', 'first', tool_name='ctx')
    agent.inject_toolcall('ses_a', 'second', tool_name='ctx')
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[-1])['content'] == 'second'


def test_remove_messages_counts_messages_not_lines(tmp_path):
    """Indices come from raw_messages, so bookkeeping lines must not shift them."""
    path = _claude_code_session(tmp_path, [
        {"type": "mode", "mode": "normal"},
        {"type": "user", "message": {"role": "user", "content": "keep me"}},
        {"type": "permission-mode", "permissionMode": "auto"},
        {"type": "assistant", "message": {"role": "assistant", "content": "drop me"}},
    ])
    removed = ClaudeCodeAgent(tmp_path).remove_messages('ses_a', [1])
    assert removed == 1
    remaining = [json.loads(l) for l in path.read_text().splitlines()]
    assert [e['type'] for e in remaining] == ['mode', 'user', 'permission-mode']


# --- JsonAgent ---

def _claude_desktop_session(tmp_path, data, session_id='ses_a'):
    d = tmp_path / 'local-agent-mode-sessions'
    d.mkdir(parents=True)
    path = d / f'{session_id}.json'
    path.write_text(json.dumps(data))
    return path


def test_claude_desktop_reads_message_list(tmp_path):
    _claude_desktop_session(tmp_path, [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    messages = ClaudeDesktopAgent(tmp_path).messages('ses_a')
    assert [(m.role, m.content) for m in messages] == [('user', 'hi'), ('assistant', 'hello')]


def test_claude_desktop_reads_wrapped_messages(tmp_path):
    _claude_desktop_session(tmp_path, {"name": "Chat", "messages": [
        {"role": "user", "content": "hi"}]})
    assert ClaudeDesktopAgent(tmp_path).sessions()[0].name == 'Chat'


def test_claude_desktop_skips_config_files(tmp_path):
    _claude_desktop_session(tmp_path, [{"role": "user", "content": "hi"}])
    (tmp_path / 'local-agent-mode-sessions' / 'manifest.json').write_text('{}')
    sessions = ClaudeDesktopAgent(tmp_path).sessions()
    assert [s.id for s in sessions] == ['ses_a']


def test_json_inject_system_round_trips(tmp_path):
    path = _claude_desktop_session(tmp_path, [{"role": "user", "content": "hi"}])
    ClaudeDesktopAgent(tmp_path).inject_system('ses_a', 'be brief')
    data = json.loads(path.read_text())
    assert data[0] == {"role": "system", "content": "be brief"}

    ClaudeDesktopAgent(tmp_path).inject_system('ses_a', 'be briefer')
    data = json.loads(path.read_text())
    assert len(data) == 2
    assert data[0]['content'] == 'be briefer'


def test_json_remove_messages(tmp_path):
    path = _claude_desktop_session(tmp_path, [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ])
    assert ClaudeDesktopAgent(tmp_path).remove_messages('ses_a', [0]) == 1
    assert json.loads(path.read_text()) == [{"role": "assistant", "content": "two"}]


# --- Codex ---

def _codex_rollout(tmp_path, entries, name='rollout-2026-01-01T00-00-00-abc123.jsonl'):
    d = tmp_path / 'sessions' / '2026' / '01'
    d.mkdir(parents=True)
    path = d / name
    path.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
    return path


def test_codex_sessions_from_rollout_header(tmp_path):
    _codex_rollout(tmp_path, [
        {"session_meta": {"id": "abc123", "title": "Fix the parser"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "go"}]}},
    ])
    sessions = CodexAgent(tmp_path).sessions()
    assert [(s.id, s.name) for s in sessions] == [('abc123', 'Fix the parser')]


def test_codex_reads_rollout_messages(tmp_path):
    """Codex had no reader at all before the agent layer; it does now."""
    _codex_rollout(tmp_path, [
        {"session_meta": {"id": "abc123"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "go"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "done"}]}},
        {"type": "event_msg", "payload": {"type": "token_count"}},
    ])
    messages = CodexAgent(tmp_path).messages('abc123')
    assert [(m.role, m.content) for m in messages] == [('user', 'go'), ('assistant', 'done')]


def test_codex_finds_session_by_uuid_in_filename(tmp_path):
    _codex_rollout(tmp_path, [{"session_meta": {"id": "abc123"}}])
    assert CodexAgent(tmp_path).session_file('abc123') is not None


# --- Pi refuses what it cannot do safely ---

@pytest.mark.parametrize('operation', ['inject_system', 'inject_toolcall', 'remove_messages'])
def test_pi_refuses_mutation(tmp_path, operation):
    agent = PiAgent(tmp_path)
    arg = [] if operation == 'remove_messages' else 'text'
    with pytest.raises(UnsupportedOperation):
        getattr(agent, operation)('ses_a', arg)


def test_unsupported_operation_names_the_agent(tmp_path):
    with pytest.raises(UnsupportedOperation) as excinfo:
        PiAgent(tmp_path).inject_system('ses_a', 'x')
    assert 'pi' in str(excinfo.value)


def test_goose_refuses_injection_rather_than_writing_the_wrong_db(tmp_path):
    """Goose is 'sqlite' like opencode but shares none of its schema."""
    with pytest.raises(UnsupportedOperation):
        GooseAgent(tmp_path).inject_system('ses_a', 'x')


# --- token_usage ---

def test_token_usage_defaults_to_none(tmp_path):
    assert ClaudeCodeAgent(tmp_path).token_usage('ses_a') is None
