import json
import stat
from pathlib import Path

import pytest

from gchat_mcp import auth, chat, setup

README = Path(__file__).resolve().parents[1] / "README.md"


@pytest.mark.parametrize("raw, want", [
    ("  ~/Downloads/client_secret_1.json \n", Path.home() / "Downloads/client_secret_1.json"),
    ("'/tmp/My Files/c.json'", Path("/tmp/My Files/c.json")),  # pasted with quotes
    ('"/tmp/a b.json"', Path("/tmp/a b.json")),
    ("/tmp/My\\ Files/c.json", Path("/tmp/My Files/c.json")),  # dragged into a macOS terminal
])
def test_clean_path(raw, want):
    assert setup.clean_path(raw) == want


def test_client_kind():
    assert setup.client_kind({"installed": {"client_id": "1.apps"}}) == "desktop"
    assert setup.client_kind({"web": {"client_id": "1.apps"}}) == "web"
    for bad in ({}, {"installed": {}}, [], {"type": "service_account", "client_id": "x"}):
        with pytest.raises(setup.Stop, match="not an OAuth client file"):
            setup.client_kind(bad)


def test_store_client_copies_private(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "cfg" / "client_secret.json")
    src = tmp_path / "client_secret_9.json"
    src.write_text(json.dumps({"installed": {"client_id": "9.apps", "client_secret": "s"}}))
    assert setup.store_client(src) == "desktop"
    assert json.loads(auth.CLIENT.read_text())["installed"]["client_id"] == "9.apps"
    assert stat.S_IMODE(auth.CLIENT.stat().st_mode) == 0o600


def test_store_client_failures_name_the_next_step(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "cfg" / "client_secret.json")
    with pytest.raises(setup.Stop, match="No file at"):
        setup.store_client(tmp_path / "missing.json")
    (tmp_path / "page.html").write_text("<html>")
    with pytest.raises(setup.Stop, match="Download the key file again"):
        setup.store_client(tmp_path / "page.html")
    assert not auth.CLIENT.exists()


def test_newest_download(tmp_path):
    assert setup.newest_download(tmp_path) is None
    old, new = tmp_path / "client_secret_a.json", tmp_path / "client_secret_b.json"
    old.write_text("{}")
    new.write_text("{}")
    import os
    os.utime(old, (1, 1))
    assert setup.newest_download(tmp_path) == new


def test_clipboard_command():
    have = {"wl-copy": False, "xclip": True, "pbcopy": True, "clip": True}
    which = lambda c: have.get(c, False)
    assert setup.clipboard_command("Linux", which) == ["xclip", "-selection", "clipboard"]
    assert setup.clipboard_command("Darwin", which) == ["pbcopy"]
    assert setup.clipboard_command("Windows", which) == ["clip"]
    assert setup.clipboard_command("Linux", lambda c: False) is None


def test_server_command_and_configs():
    cmd = setup.server_command(lambda c: "/opt/bin/uvx" if c == "uvx" else None)
    assert cmd == ["/opt/bin/uvx", "--python", "3.13", "google-chat-mcp"]
    text = setup.configs(cmd)
    assert "claude mcp add -s user gchat -- uvx --python 3.13 google-chat-mcp" in text
    assert "codex mcp add gchat -- uvx --python 3.13 google-chat-mcp" in text
    block = json.loads(text[text.index("{"):text.index("}\n}") + 3])
    assert block == {"mcpServers": {"gchat": {"command": "/opt/bin/uvx", "args": ["--python", "3.13", "google-chat-mcp"]}}}
    no_uvx = setup.server_command(lambda c: "/home/u/.local/bin/gchat-mcp" if c == "gchat-mcp" else None)
    assert no_uvx == [str(Path("/home/u/.local/bin/gchat-mcp").resolve())]


def test_prog():
    assert setup.prog("/home/u/.cache/uv/archive-v0/AbC") == "uvx --python 3.13 google-chat-mcp"
    assert setup.prog("/home/u/.local/share/uv/tools/google-chat-mcp") == "gchat-mcp"


@pytest.mark.parametrize("status, msg, reason, step", [
    (403, "Google Chat API has not been used in project 1 before or it is disabled.", "SERVICE_DISABLED", "step 2"),
    (404, "Google Chat app not found. To create a Chat app, you must turn on the Chat API.", "", "step 3"),
    (401, "Request had invalid authentication credentials.", "", "login"),
    (500, "Internal error", "", "HTTP 500"),
])
def test_explain_api_error(status, msg, reason, step):
    assert step in setup.explain_api_error(chat.ApiError("spaces.list", status, msg, reason))


def test_check_without_key_file(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "client_secret.json")
    with pytest.raises(setup.Stop, match="no key file"):
        setup.check()


def test_non_interactive_without_key_file_stops(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "client_secret.json")
    assert setup.main(["--non-interactive"]) == 1
    assert "--client-secret" in capsys.readouterr().out


def test_agent_prompt_is_the_readme_copy():
    """The README's copy block and `setup --agent-prompt` are the same text."""
    readme = README.read_text()
    start = readme.index("<!-- agent-prompt:start -->")
    block = readme[start:readme.index("<!-- agent-prompt:end -->")]
    body = block[block.index("```text\n") + 8:block.rindex("```")]
    assert body == setup.prompt_text()


@pytest.mark.parametrize("error, says", [
    (auth.WSGITimeoutError("stray request"), "did not come back from Google"),
    (OSError(48, "Address already in use"), "Close any other"),
    (ValueError("(invalid_grant) Bad Request"), "click Allow"),
])
def test_login_failures_say_what_to_do(tmp_path, monkeypatch, error, says):
    client = tmp_path / "client_secret.json"
    client.write_text(json.dumps({"installed": {"client_id": "1.apps", "client_secret": "s",
                                                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                                "token_uri": "https://oauth2.googleapis.com/token"}}))
    monkeypatch.setattr(auth, "CLIENT", client)
    monkeypatch.setattr(auth, "TOKEN", tmp_path / "token.json")

    def fail(self, **kw):
        raise error
    monkeypatch.setattr(auth.InstalledAppFlow, "run_local_server", fail)
    with pytest.raises(SystemExit, match=says):
        auth.login()
    assert not (tmp_path / "token.json").exists()
