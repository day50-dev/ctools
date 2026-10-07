"""Tests for the Cline, omp, and Freebuff agents."""

import json

import pytest

from ctools.agents import (
    ClineAgent,
    FreebuffAgent,
    OmpAgent,
    SessionNotFound,
    get_agent,
)


# --- Registry / lookup ---

def test_cline_registered():
    agent = get_agent('cline')
    assert agent is not None
    assert agent.name == 'cline'
    assert agent.label == 'Cline'
    assert agent.storage_format == 'json'


def test_omp_registered():
    agent = get_agent('omp')
    assert agent is not None
    assert agent.name == 'omp'
    assert agent.label == 'omp'
    assert agent.storage_format == 'jsonl'


def test_freebuff_registered():
    agent = get_agent('freebuff')
    assert agent is not None
    assert agent.name == 'freebuff'
    assert agent.label == 'Freebuff'
    assert agent.storage_format == 'json'


# --- Cline ---

def _make_cline_task(tmp_path, task_id='1700000000000',
                     history=None, meta=None):
    task_dir = tmp_path / 'data' / 'tasks' / task_id
    task_dir.mkdir(parents=True)
    if history is None:
        history = [
            {'role': 'user', 'content': json.dumps(
                [{'type': 'text', 'text': 'hello cline'}])},
            {'role': 'assistant', 'content': json.dumps(
                [{'type': 'text', 'text': 'hi there'},
                 {'type': 'thinking', 'thinking': 'should be dropped'}])},
        ]
    (task_dir / 'api_conversation_history.json').write_text(json.dumps(history))
    if meta is None:
        meta = {'model_usage': [{'ts': 1700000000000, 'model_id': 'cline-model',
                                 'model_provider_id': 'cline'}],
                'environment_history': [], 'files_in_context': []}
    (task_dir / 'task_metadata.json').write_text(json.dumps(meta))
    (task_dir / 'ui_messages.json').write_text(json.dumps(
        [{'ts': 1700000000000, 'type': 'user'}, {'ts': 1700000060000, 'type': 'assistant'}]))
    return task_dir


def test_cline_lists_sessions(tmp_path):
    _make_cline_task(tmp_path)
    agent = ClineAgent(tmp_path)
    sessions = agent.sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == '1700000000000'
    assert s.model == 'cline-model'
    assert s.message_count == 2
    assert s.ctime is not None
    assert s.name == 'hello cline'


def test_cline_reads_messages_drops_thinking(tmp_path):
    _make_cline_task(tmp_path)
    agent = ClineAgent(tmp_path)
    msgs = agent.messages('1700000000000')
    assert [(m.role, m.content) for m in msgs] == [
        ('user', 'hello cline'),
        ('assistant', 'hi there'),
    ]


def test_cline_plain_string_content(tmp_path):
    history = [
        {'role': 'user', 'content': 'plain text prompt'},
        {'role': 'assistant', 'content': 'plain text reply'},
    ]
    _make_cline_task(tmp_path, history=history)
    agent = ClineAgent(tmp_path)
    assert [m.content for m in agent.messages('1700000000000')] == [
        'plain text prompt', 'plain text reply']


def test_cline_history_item_title_and_tokens(tmp_path):
    task_dir = _make_cline_task(tmp_path)
    (task_dir / 'history_item.json').write_text(json.dumps(
        {'task': 'Nice Task Title', 'ts': 1700000000123,
         'tokensIn': 100, 'tokensOut': 50, 'workspace': '/repo'}))
    agent = ClineAgent(tmp_path)
    s = agent.sessions()[0]
    assert s.name == 'Nice Task Title'
    assert s.size == 150
    assert s.path == '/repo'


def test_cline_missing_dir_yields_no_sessions(tmp_path):
    assert ClineAgent(tmp_path).sessions() == []


def test_cline_unknown_session_raises(tmp_path):
    _make_cline_task(tmp_path)
    with pytest.raises(SessionNotFound):
        ClineAgent(tmp_path).messages('nope')


# --- omp ---

def _write_omp_file(path, records):
    with open(path, 'w') as f:
        for record in records:
            f.write(json.dumps(record) + '\n')


def _omp_session_records(session_id='019700f1-0000-7c3d-8e4f-aabbccddeeff',
                         entries=None, title='refactor importer',
                         ts='2026-05-14T10:12:03.000Z', cwd='/repo'):
    title_rec = {'type': 'title', 'v': 1, 'title': title, 'source': 'user',
                 'updatedAt': ts, 'pad': ' '}
    header = {'type': 'session', 'version': 3, 'id': session_id,
              'timestamp': ts, 'cwd': cwd, 'title': title, 'titleSource': 'user'}
    return [title_rec, header] + (entries or [])


def _omp_message(eid, parent, role, text, ts_ms, model=None, tokens=None):
    message = {'role': role, 'content': [{'type': 'text', 'text': text}],
               'timestamp': ts_ms}
    if model:
        message['model'] = model
    if tokens is not None:
        message['usage'] = {'input': 10, 'output': 5, 'totalTokens': tokens}
    return {'type': 'message', 'id': eid, 'parentId': parent,
            'timestamp': '2026-05-14T10:12:05.000Z', 'message': message}


def _make_omp_session(tmp_path, subdir='proj', **kwargs):
    d = tmp_path / 'agent' / 'sessions' / subdir
    d.mkdir(parents=True)
    path = d / '1700000000000_abc.jsonl'
    _write_omp_file(path, _omp_session_records(**kwargs))
    return path


def test_omp_lists_sessions(tmp_path):
    entries = [
        _omp_message('1', None, 'user', 'do the thing', 1778753525000),
        _omp_message('2', '1', 'assistant', 'on it', 1778753526000,
                     model='claude-opus-4-6', tokens=1245),
        _omp_message('3', '2', 'toolResult', 'done', 1778753527000),
    ]
    _make_omp_session(tmp_path, entries=entries)
    agent = OmpAgent(tmp_path)
    sessions = agent.sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.name == 'refactor importer'
    assert s.model == 'claude-opus-4-6'
    assert s.size == 1245
    assert s.path == '/repo'
    assert s.message_count == 3
    assert s.ctime is not None


def test_omp_reads_main_conversation(tmp_path):
    entries = [
        _omp_message('1', None, 'user', 'do the thing', 1778753525000),
        _omp_message('2', '1', 'assistant', 'on it', 1778753526000),
        _omp_message('3', '2', 'toolResult', 'done', 1778753527000),
    ]
    _make_omp_session(tmp_path, entries=entries)
    agent = OmpAgent(tmp_path)
    session_id = agent.sessions()[0].id
    msgs = agent.messages(session_id)
    assert [(m.role, m.content) for m in msgs] == [
        ('user', 'do the thing'), ('assistant', 'on it')]
    # raw includes the tool result
    assert any(m.role == 'toolResult' for m in agent.raw_messages(session_id))


def test_omp_follows_leaf_branch_not_side_branch(tmp_path):
    # main branch: 1 -> 2 -> 4 (leaf); side branch: 2 -> 3
    entries = [
        _omp_message('1', None, 'user', 'start', 1),
        _omp_message('2', '1', 'assistant', 'main mid', 2),
        _omp_message('3', '2', 'assistant', 'side branch', 3),
        _omp_message('4', '2', 'assistant', 'leaf', 4),
    ]
    _make_omp_session(tmp_path, entries=entries)
    agent = OmpAgent(tmp_path)
    session_id = agent.sessions()[0].id
    contents = [m.content for m in agent.messages(session_id)]
    assert 'leaf' in contents
    assert 'side branch' not in contents


def test_omp_ignores_non_message_entries(tmp_path):
    entries = [
        _omp_message('1', None, 'user', 'hi', 1),
        {'type': 'mode_change', 'id': 'm', 'parentId': '1',
         'timestamp': '2026-05-14T10:12:06.000Z', 'mode': 'plan'},
        _omp_message('2', '1', 'assistant', 'reply', 2),
    ]
    _make_omp_session(tmp_path, entries=entries)
    agent = OmpAgent(tmp_path)
    session_id = agent.sessions()[0].id
    assert [m.content for m in agent.messages(session_id)] == ['hi', 'reply']


def test_omp_title_record_overrides_header(tmp_path):
    entries = [_omp_message('1', None, 'user', 'hi', 1)]
    _make_omp_session(tmp_path, entries=entries, title='physical title wins')
    agent = OmpAgent(tmp_path)
    assert agent.sessions()[0].name == 'physical title wins'


def test_omp_missing_dir_yields_no_sessions(tmp_path):
    assert OmpAgent(tmp_path).sessions() == []


def test_omp_unknown_session_raises(tmp_path):
    _make_omp_session(tmp_path)
    with pytest.raises(SessionNotFound):
        OmpAgent(tmp_path).messages('nope')


# --- Freebuff ---

def _make_freebuff_chat(tmp_path, chat_id='2026-08-29T19-24-39.712Z',
                        messages=None, meta=None):
    chat_dir = tmp_path / 'projects' / 'p1' / 'chats' / chat_id
    chat_dir.mkdir(parents=True)
    if messages is None:
        messages = [
            {'id': 'u1', 'variant': 'user', 'content': 'hi freebuff',
             'timestamp': '12:25 PM'},
            {'id': 'a1', 'variant': 'ai', 'content': '',
             'blocks': [
                 {'type': 'text', 'content': 'first text', 'textType': 'text'},
                 {'type': 'text', 'content': 'hidden reasoning',
                  'textType': 'reasoning'},
                 {'type': 'agent', 'agentName': 'sub',
                  'blocks': [{'type': 'text', 'content': 'nested sub text',
                              'textType': 'text'}]},
             ], 'timestamp': '12:26 PM', 'isComplete': True},
        ]
    (chat_dir / 'chat-messages.json').write_text(json.dumps(messages))
    if meta is None:
        meta = {'messageCount': 2, 'firstPrompt': 'hi freebuff',
                'messagesSize': 999, 'messagesMtimeMs': 1788031975172}
    (chat_dir / 'chat-meta.json').write_text(json.dumps(meta))
    return chat_dir


def test_freebuff_lists_sessions(tmp_path):
    _make_freebuff_chat(tmp_path)
    agent = FreebuffAgent(tmp_path)
    sessions = agent.sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == '2026-08-29T19-24-39.712Z'
    assert s.name == 'hi freebuff'
    assert s.message_count == 2
    assert s.size == 999
    assert s.mtime is not None


def test_freebuff_reads_messages_includes_nested_excludes_reasoning(tmp_path):
    _make_freebuff_chat(tmp_path)
    agent = FreebuffAgent(tmp_path)
    session_id = agent.sessions()[0].id
    msgs = agent.messages(session_id)
    user, assistant = msgs
    assert user.role == 'user'
    assert user.content == 'hi freebuff'
    assert assistant.role == 'assistant'
    assert 'first text' in assistant.content
    assert 'nested sub text' in assistant.content
    assert 'hidden reasoning' not in assistant.content


def test_freebuff_user_ai_variant_mapping(tmp_path):
    messages = [
        {'id': 'u', 'variant': 'user', 'content': 'q'},
        {'id': 'a', 'variant': 'ai', 'content': 'a'},
        {'id': 'x', 'variant': 'system', 'content': 'ignore me'},
    ]
    _make_freebuff_chat(tmp_path, messages=messages)
    agent = FreebuffAgent(tmp_path)
    session_id = agent.sessions()[0].id
    assert [(m.role, m.content) for m in agent.messages(session_id)] == [
        ('user', 'q'), ('assistant', 'a')]


def test_freebuff_missing_dir_yields_no_sessions(tmp_path):
    assert FreebuffAgent(tmp_path).sessions() == []


def test_freebuff_unknown_session_raises(tmp_path):
    _make_freebuff_chat(tmp_path)
    with pytest.raises(SessionNotFound):
        FreebuffAgent(tmp_path).messages('nope')
