import json
import pytest
from datetime import datetime
from pathlib import Path
from ctools.lib import (
    Session, Agent, Match, Message, AGENTS,
    JsonFormatter, XmlFormatter, MarkdownFormatter,
    get_formatter, format_size, format_datetime
)


# --- Helper fixtures ---

@pytest.fixture
def sample_sessions():
    return [
        Session(
            id="ses_abc123",
            name="Python Help Session",
            ctime=datetime(2024, 1, 15, 10, 30),
            mtime=datetime(2024, 1, 15, 11, 45),
            size=2500,
            model="gpt-4",
            message_count=12
        ),
        Session(
            id="ses_def456",
            name="Code Review",
            ctime=datetime(2024, 1, 16, 9, 0),
            mtime=datetime(2024, 1, 16, 9, 30),
            size=1200,
            model="claude-3",
            message_count=8
        ),
    ]


@pytest.fixture
def sample_messages():
    return [
        Message(role="user", content="Hello, can you help me with Python?"),
        Message(role="assistant", content="Of course! What do you need help with?"),
        Message(role="user", content="I need to parse JSON files"),
    ]


@pytest.fixture
def sample_matches():
    return [
        Match(
            session_id="ses_abc123",
            agent="opencode",
            line_num=5,
            line="user: I need to parse JSON files",
            context_before=["assistant: Of course! What do you need help with?"],
            context_after=["assistant: You can use the json module."]
        ),
        Match(
            session_id="ses_abc123",
            agent="opencode",
            line_num=12,
            line="assistant: You can use the json module."
        ),
    ]


# --- Helper function tests ---

def test_format_size_bytes():
    assert format_size(512) == "512 B"


def test_format_size_kb():
    assert format_size(2048) == "2.0 KB"


def test_format_size_mb():
    assert format_size(2048000) == "2.0 MB"


def test_format_datetime():
    dt = datetime(2024, 1, 15, 10, 30)
    assert format_datetime(dt) == "2024-01-15 10:30"


def test_format_datetime_none():
    assert format_datetime(None) == "N/A"


# --- Formatter factory tests ---

def test_get_formatter_json():
    fmt = get_formatter('json')
    assert isinstance(fmt, JsonFormatter)


def test_get_formatter_xml():
    fmt = get_formatter('xml')
    assert isinstance(fmt, XmlFormatter)


def test_get_formatter_md():
    fmt = get_formatter('md')
    assert isinstance(fmt, MarkdownFormatter)


def test_get_formatter_invalid():
    with pytest.raises(ValueError) as exc_info:
        get_formatter('csv')
    assert "Unknown format" in str(exc_info.value)


# --- JSON Formatter tests ---

def test_json_format_sessions(sample_sessions):
    fmt = JsonFormatter()
    output = fmt.format_sessions(sample_sessions, "opencode")
    data = json.loads(output)
    
    assert len(data) == 2
    assert data[0]['id'] == 'ses_abc123'
    assert data[0]['name'] == 'Python Help Session'
    assert data[0]['ctime'] == '2024-01-15T10:30:00'
    assert data[0]['size'] == 2500
    assert data[0]['model'] == 'gpt-4'
    assert data[0]['message_count'] == 12


def test_json_format_session_export(sample_messages):
    fmt = JsonFormatter()
    output = fmt.format_session_export(sample_messages, "ses_abc123", "opencode")
    data = json.loads(output)
    
    assert len(data) == 3
    assert data[0] == {'role': 'user', 'content': 'Hello, can you help me with Python?'}
    assert data[1] == {'role': 'assistant', 'content': 'Of course! What do you need help with?'}


def test_json_format_matches(sample_matches):
    fmt = JsonFormatter()
    output = fmt.format_matches(sample_matches)
    data = json.loads(output)
    
    assert len(data) == 2
    assert data[0]['session'] == 'opencode/ses_abc123'
    assert data[0]['line_num'] == 5
    assert data[0]['line'] == 'user: I need to parse JSON files'
    assert 'context_before' in data[0]
    assert 'context_after' in data[0]
    assert 'context_before' not in data[1]  # No context for second match


def test_json_format_match_files():
    fmt = JsonFormatter()
    output = fmt.format_match_files(['opencode/ses_abc', 'opencode/ses_def'])
    data = json.loads(output)
    
    assert len(data) == 2
    assert 'opencode/ses_abc' in data


def test_json_format_match_counts():
    fmt = JsonFormatter()
    output = fmt.format_match_counts({'opencode/ses_abc': 5, 'opencode/ses_def': 2})
    data = json.loads(output)
    
    assert data['opencode/ses_abc'] == 5
    assert data['opencode/ses_def'] == 2


# --- XML Formatter tests ---

def test_xml_format_sessions(sample_sessions):
    fmt = XmlFormatter()
    output = fmt.format_sessions(sample_sessions, "opencode")
    
    assert '<?xml version="1.0"' in output
    assert '<sessions agent="opencode">' in output
    assert '<session id="ses_abc123"' in output
    assert 'name="Python Help Session"' in output
    assert 'size="2500"' in output


def test_xml_format_session_export(sample_messages):
    fmt = XmlFormatter()
    output = fmt.format_session_export(sample_messages, "ses_abc123", "opencode")
    
    assert '<?xml version="1.0"' in output
    assert '<session id="ses_abc123" agent="opencode">' in output
    assert '<message role="user">' in output
    assert 'Hello, can you help me with Python?' in output


def test_xml_format_matches(sample_matches):
    fmt = XmlFormatter()
    output = fmt.format_matches(sample_matches)
    
    assert '<?xml version="1.0"' in output
    assert '<matches>' in output
    assert '<session id="ses_abc123" agent="opencode">' in output
    assert '<match line="5">' in output
    assert '<message>' in output
    assert '<context_before>' in output
    assert '<context_after>' in output


def test_xml_format_match_files():
    fmt = XmlFormatter()
    output = fmt.format_match_files(['opencode/ses_abc', 'opencode/ses_def'])
    
    assert '<?xml version="1.0"' in output
    assert '<files match="true">' in output
    assert '<file agent="opencode" session="ses_abc">' in output


def test_xml_format_match_files_no_match():
    fmt = XmlFormatter()
    output = fmt.format_match_files(['opencode/ses_abc'], has_matches=False)
    
    assert 'match="false"' in output


def test_xml_format_match_counts():
    fmt = XmlFormatter()
    output = fmt.format_match_counts({'opencode/ses_abc': 5})
    
    assert '<?xml version="1.0"' in output
    assert '<counts>' in output
    assert '<count agent="opencode" session="ses_abc" matches="5">' in output


# --- Markdown Formatter tests ---

def test_md_format_sessions(sample_sessions):
    fmt = MarkdownFormatter()
    output = fmt.format_sessions(sample_sessions, "opencode")
    
    assert '# Sessions — opencode' in output
    assert '| ID | Name | Modified | Size | Messages |' in output
    assert '| `ses_abc123` |' in output
    assert '2024-01-15 11:45' in output
    assert '2.4 KB' in output
    assert '*2 session(s)*' in output


def test_md_format_sessions_empty():
    fmt = MarkdownFormatter()
    output = fmt.format_sessions([], "opencode")
    
    assert '# Sessions — opencode' in output
    assert '*No sessions found*' in output


def test_md_format_session_export(sample_messages):
    fmt = MarkdownFormatter()
    output = fmt.format_session_export(sample_messages, "ses_abc123", "opencode")
    
    assert '# Session — opencode/ses_abc123' in output
    assert '## You' in output
    assert 'Hello, can you help me with Python?' in output
    assert '## Assistant' in output


def test_md_format_matches(sample_matches):
    fmt = MarkdownFormatter()
    output = fmt.format_matches(sample_matches)
    
    assert '### opencode/ses_abc123' in output
    assert 'assistant: Of course! What do you need help with?' in output
    assert '**5:** user: I need to parse JSON files' in output
    assert 'assistant: You can use the json module.' in output


def test_md_format_match_files():
    fmt = MarkdownFormatter()
    output = fmt.format_match_files(['opencode/ses_abc', 'opencode/ses_def'])
    
    assert '# Files with matches' in output
    assert '- `opencode/ses_abc`' in output
    assert '- `opencode/ses_def`' in output


def test_md_format_match_files_no_match():
    fmt = MarkdownFormatter()
    output = fmt.format_match_files(['opencode/ses_abc'], has_matches=False)
    
    assert '# Files without matches' in output


def test_md_format_match_counts():
    fmt = MarkdownFormatter()
    output = fmt.format_match_counts({'opencode/ses_abc': 5, 'opencode/ses_def': 2})
    
    assert '# Match Counts' in output
    assert '- `opencode/ses_abc`: 5' in output
    assert '- `opencode/ses_def`: 2' in output


# --- Agent registry tests ---

def test_agents_in_lib():
    """Test that AGENTS is accessible from lib."""
    assert 'claude' in AGENTS
    assert 'claude-code' in AGENTS
    assert 'opencode' in AGENTS
    assert 'codex' in AGENTS


def test_agent_fields():
    """Test that agents have required fields."""
    for name, agent in AGENTS.items():
        assert agent.name == name
        assert agent.description
        assert agent.base_path
        assert agent.storage_format in ('json', 'sqlite', 'jsonl')
