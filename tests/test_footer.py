"""Configurable footer behaviour through the real chat._send path against tests/transport.ChatServer."""
import json
import types

import pytest

from gchat_mcp import chat, server, state
from tests.transport import ChatServer, ME

SP = "spaces/AAA"
FOOTER = "_Sent by Ajmal Aksar_"


@pytest.fixture
def srv(monkeypatch):
    s = ChatServer()
    monkeypatch.setattr(chat, "_acquire", lambda: (s, "grant-1"))
    monkeypatch.setattr(chat, "time", types.SimpleNamespace(sleep=lambda t: None))
    chat._identities.clear()
    monkeypatch.setattr(chat, "label", lambda sp: "Team")
    monkeypatch.setattr(chat, "who", lambda u: "Ajmal Aksar")
    return s


def created_text(srv) -> str:
    return next(x[3]["text"] for x in srv.log if x[0] == "POST" and x[1] == f"v1/{SP}/messages")


def test_default_is_off(srv):
    out = server.send_message("AAA", "hello")
    assert created_text(srv) == "hello"
    assert "footer added: no" in out


def test_env_turns_footer_on_and_expands_user(srv, monkeypatch):
    monkeypatch.setenv("GCHAT_MCP_FOOTER", "_Sent by {user}_")
    out = server.send_message("AAA", "hello")
    assert created_text(srv) == f"hello\n\n{FOOTER}"
    assert "footer added: yes" in out


def test_settings_file_turns_footer_on(srv, monkeypatch, tmp_path):
    monkeypatch.setenv("GCHAT_MCP_STATE", "file")
    monkeypatch.setenv("GCHAT_MCP_STATE_DIR", str(tmp_path / "state"))
    state.reset()
    settings = server.inbox_settings(footer="_via settings_")
    assert "Footer: _via settings_" in settings
    saved = json.loads((tmp_path / "state" / f"{state.user_key(ME)}.json").read_text())
    assert saved["settings"]["footer"] == "_via settings_"
    server.send_message("AAA", "hello")
    assert created_text(srv) == "hello\n\n_via settings_"


def test_env_overrides_settings(srv, monkeypatch):
    server.inbox_settings(footer="_from settings_")
    monkeypatch.setenv("GCHAT_MCP_FOOTER", "_from env_")
    server.send_message("AAA", "hello")
    assert created_text(srv) == "hello\n\n_from env_"


@pytest.mark.parametrize("disabled", ["", "off"])
def test_env_empty_or_off_overrides_settings(srv, monkeypatch, disabled):
    server.inbox_settings(footer="_from settings_")
    monkeypatch.setenv("GCHAT_MCP_FOOTER", disabled)
    out = server.send_message("AAA", "hello")
    assert created_text(srv) == "hello"
    assert "footer added: no" in out


def test_settings_off_disables_a_stored_footer(srv):
    server.inbox_settings(footer="_from settings_")
    server.inbox_settings(footer="off")
    out = server.send_message("AAA", "hello")
    assert created_text(srv) == "hello"
    assert "footer added: no" in out


def test_footer_is_never_doubled(srv, monkeypatch):
    monkeypatch.setenv("GCHAT_MCP_FOOTER", FOOTER)
    already = f"hello\n\n{FOOTER}"
    out = server.send_message("AAA", already)
    assert created_text(srv) == already
    assert "footer added: no" in out


def test_file_caption_gets_footer(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GCHAT_MCP_FOOTER", FOOTER)
    f = tmp_path / "report.txt"
    f.write_text("hello")
    out = server.send_file("AAA", str(f), "caption")
    assert created_text(srv) == f"caption\n\n{FOOTER}"
    assert "footer added: yes" in out


def test_file_without_caption_still_carries_footer(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GCHAT_MCP_FOOTER", FOOTER)
    f = tmp_path / "report.txt"
    f.write_text("hello")
    server.send_file("AAA", str(f))
    assert created_text(srv) == FOOTER


def test_edit_gets_footer_once(srv, monkeypatch):
    monkeypatch.setenv("GCHAT_MCP_FOOTER", FOOTER)
    name = f"{SP}/messages/mine"
    srv.messages[name] = {"name": name, "sender": {"name": f"users/{ME}", "type": "HUMAN"},
                          "text": "old", "createTime": "2026-11-23T13:30:00Z",
                          "thread": {"name": f"{SP}/threads/T1"}}
    out = server.edit_message(name, "changed")
    patch = next(x for x in srv.log if x[0] == "PATCH" and x[1] == f"v1/{name}")
    assert patch[3]["text"] == f"changed\n\n{FOOTER}"
    assert "footer added: yes" in out

    out = server.edit_message(name, f"changed again\n\n{FOOTER}")
    patch = [x for x in srv.log if x[0] == "PATCH" and x[1] == f"v1/{name}"][-1]
    assert patch[3]["text"] == f"changed again\n\n{FOOTER}"
    assert "footer added: no" in out
