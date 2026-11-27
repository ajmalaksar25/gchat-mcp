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


def test_store_client_in_place_is_made_private(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "client_secret.json")
    auth.CLIENT.write_text(json.dumps({"installed": {"client_id": "1.apps"}}))
    auth.CLIENT.chmod(0o644)
    setup.store_client(auth.CLIENT)
    assert stat.S_IMODE(auth.CLIENT.stat().st_mode) == 0o600


def test_new_client_forces_a_new_sign_in(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "cfg/client_secret.json")
    monkeypatch.setattr(auth, "TOKEN", tmp_path / "cfg/token.json")
    auth.TOKEN.parent.mkdir()
    auth.TOKEN.write_text(json.dumps({"client_id": "old.apps", "refresh_token": "r"}))
    new = tmp_path / "client_secret_new.json"
    new.write_text(json.dumps({"installed": {"client_id": "new.apps"}}))
    args = setup.parser().parse_args(["--client-secret", str(new)])
    setup.get_client(args, interactive=False)
    assert args.relogin
    args = setup.parser().parse_args(["--client-secret", str(new)])
    auth.TOKEN.write_text(json.dumps({"client_id": "new.apps", "refresh_token": "r"}))
    setup.get_client(args, interactive=False)
    assert not args.relogin


def test_footer_with_skip_login_and_no_sign_in_stops(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "client_secret.json")
    monkeypatch.setattr(auth, "TOKEN", tmp_path / "token.json")
    auth.CLIENT.write_text(json.dumps({"installed": {"client_id": "1.apps"}}))
    assert setup.main(["--non-interactive", "--skip-login", "--footer", "_sent by me_"]) == 1
    assert "needs a sign-in first" in capsys.readouterr().out


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


def test_server_command_and_configs(tmp_path):
    which = {"uvx": "/opt/bin/uvx", "uv": "/opt/bin/uv"}.get
    installed = tmp_path / "lib/python3.13/site-packages/gchat_mcp/setup.py"
    cmd = setup.server_command(which, installed, "/home/u/.cache/uv/archive-v0/AbC")
    assert cmd == ["/opt/bin/uvx", "--python", "3.13", "google-chat-mcp"]
    text = setup.configs(cmd, which)
    assert "claude mcp add -s user gchat -- uvx --python 3.13 google-chat-mcp" in text
    assert "codex mcp add gchat -- uvx --python 3.13 google-chat-mcp" in text
    block = json.loads(text[text.index("{"):text.index("}\n}") + 3])
    assert block == {"mcpServers": {"gchat": {"command": "/opt/bin/uvx", "args": ["--python", "3.13", "google-chat-mcp"]}}}
    assert "-- /opt/bin/uvx --python" in setup.configs(cmd, lambda c: None)  # uvx not on PATH: keep the full path


def test_server_command_source_checkout(tmp_path):
    root = tmp_path / "My Code" / "gchat-mcp"
    (root / "src/gchat_mcp").mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    which = {"uvx": "/opt/bin/uvx", "uv": "/opt/bin/uv"}.get
    cmd = setup.server_command(which, root / "src/gchat_mcp/setup.py", "/x/.venv")
    assert cmd == ["/opt/bin/uv", "run", "--directory", str(root), "gchat-mcp"]
    assert f"-- uv run --directory '{root}' gchat-mcp" in setup.configs(cmd, which)  # quoted: the path has a space


def test_server_command_installed_tool(tmp_path):
    here = tmp_path / "tools/google-chat-mcp/lib/python3.13/site-packages/gchat_mcp/setup.py"
    which = {"uvx": "/opt/bin/uvx", "gchat-mcp": "/home/u/.local/bin/gchat-mcp"}.get
    assert setup.server_command(which, here, "/home/u/.local/share/uv/tools/google-chat-mcp") == [
        str(Path("/home/u/.local/bin/gchat-mcp").resolve())]


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


def test_footer_not_saved_under_an_old_sign_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "cfg/client_secret.json")
    monkeypatch.setattr(auth, "TOKEN", tmp_path / "cfg/token.json")
    auth.TOKEN.parent.mkdir()
    auth.TOKEN.write_text(json.dumps({"client_id": "old.apps", "refresh_token": "r"}))
    new = tmp_path / "client_secret_new.json"
    new.write_text(json.dumps({"installed": {"client_id": "new.apps"}}))
    argv = ["--non-interactive", "--skip-login", "--client-secret", str(new), "--footer", "_x_"]
    assert setup.main(argv) == 1
    assert "needs a new sign-in" in capsys.readouterr().out


def test_configs_for_chosen_apps_only():
    cmd = ["/opt/bin/uvx", "--python", "3.13", "google-chat-mcp"]
    which = {"uvx": "/opt/bin/uvx"}.get
    text = setup.configs(cmd, which, ["claude-desktop", "claude-web"])
    assert "Claude Desktop: merge into" in text and "needs a hosted copy" in text and setup.REMOTE_DOC in text
    assert "claude mcp add" not in text and "codex mcp add" not in text
    assert "/opt/bin/uvx --python 3.13 google-chat-mcp" in setup.configs(cmd, which, ["other"])
    assert all(label.split()[0] in setup.configs(cmd, which) for label in setup.APPS.values())


@pytest.mark.parametrize("text, want", [("all", list(setup.APPS)), ("claude-web, cursor", ["claude-web", "cursor"]),
                                        ("codex,codex", ["codex"])])
def test_parse_apps(text, want):
    assert setup.parse_apps(text) == want


def test_parse_apps_rejects_unknown_names(capsys):
    with pytest.raises(SystemExit):
        setup.main(["--print-config", "--for", "claud-code"])
    assert "unknown app claud-code" in capsys.readouterr().err


def test_choose_apps_menu(monkeypatch, capsys):
    answers = iter(["9", "x", "2,3"])
    monkeypatch.setattr("builtins.input", lambda q: next(answers))
    assert setup.choose_apps() == ["claude-desktop", "claude-web"]
    assert capsys.readouterr().out.count("Type numbers from 1") == 2
    monkeypatch.setattr("builtins.input", lambda q: "")
    assert setup.choose_apps() == list(setup.APPS)
