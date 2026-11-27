"""`setup --gcloud`: steps 1-2 through a fake gcloud; nothing here runs the real CLI."""
import json

import pytest

from gchat_mcp import auth, gcloud, setup


class FakeGcloud:
    def __init__(self, account="sam@example.com", projects=(), enabled=(), create_error=""):
        self.account, self.projects, self.enabled = account, set(projects), set(enabled)
        self.create_error, self.calls, self.expired = create_error, [], False

    def __call__(self, args, timeout=300):
        self.calls.append(args)
        if args[:2] == ["auth", "list"]:
            return self.account
        if args[:2] == ["auth", "print-access-token"]:
            if self.expired:
                raise gcloud.Failed("Reauthentication failed. cannot prompt during non-interactive execution.")
            return "ya29.x"
        if args[:2] == ["projects", "describe"]:
            if args[2] not in self.projects:
                raise gcloud.Failed("not found")
            return args[2]
        if args[:2] == ["projects", "create"]:
            if self.create_error:
                raise gcloud.Failed(self.create_error)
            self.projects.add(args[2])
            return ""
        if args[:3] == ["services", "list", "--enabled"]:
            return json.dumps([{"config": {"name": n}} for n in self.enabled])
        if args[:2] == ["services", "enable"]:
            self.enabled |= {a for a in args[2:] if a.endswith(".googleapis.com")}
            return ""
        raise AssertionError(args)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DIR", tmp_path)
    monkeypatch.setattr(auth, "CLIENT", tmp_path / "client_secret.json")
    monkeypatch.setattr(gcloud, "PROJECT_FILE", tmp_path / "gcloud_project")
    return tmp_path


@pytest.fixture
def fake(monkeypatch):
    f = FakeGcloud()
    monkeypatch.setattr(gcloud, "run", f)
    monkeypatch.setattr(gcloud, "available", lambda which=None: True)
    return f


@pytest.mark.parametrize("email, says", [("", "not signed in"), ("me@gmail.com", "personal Gmail")])
def test_account_must_be_workspace(email, says):
    with pytest.raises(gcloud.Failed, match=says):
        gcloud.check_account(email)


def test_creates_a_project_once_and_remembers_it(home):
    g = FakeGcloud()
    project, created = gcloud.ensure_project(gc=g)
    assert created and gcloud.PROJECT_RE.match(project) and gcloud.remembered() == project
    again, created2 = gcloud.ensure_project(gc=g)
    assert (again, created2) == (project, False)
    assert sum(c[:2] == ["projects", "create"] for c in g.calls) == 1


def test_given_project_must_exist(home):
    with pytest.raises(gcloud.Failed, match="does not exist"):
        gcloud.ensure_project("missing-proj", gc=FakeGcloud())
    with pytest.raises(gcloud.Failed, match="not a Google Cloud project id"):
        gcloud.ensure_project("Bad_Id", gc=FakeGcloud())


def test_org_policy_refusal_says_what_to_do(home):
    with pytest.raises(gcloud.Failed, match="--project ID"):
        gcloud.ensure_project(gc=FakeGcloud(create_error="constraints/resourcemanager.projectCreation"))


def test_enables_only_missing_apis():
    g = FakeGcloud(enabled={"chat.googleapis.com"})
    assert gcloud.enable_apis("p-123456", gc=g) == ["people.googleapis.com"]
    assert gcloud.enable_apis("p-123456", gc=g) == []


def test_links_select_the_project():
    assert gcloud.with_project("https://x/a", "p-1") == "https://x/a?project=p-1"
    assert gcloud.with_project("https://x/a?apiid=b", "p-1") == "https://x/a?apiid=b&project=p-1"
    assert gcloud.with_project("https://x/a", "") == "https://x/a"


def test_setup_gcloud_non_interactive_does_steps_1_2_and_prints_3_to_5(home, fake, capsys):
    assert setup.main(["--gcloud", "--non-interactive"]) == 1  # stops for the key file, as without gcloud
    out = capsys.readouterr().out
    project = gcloud.remembered()
    assert f"project {project} created (gcloud)" in out and "turned on (gcloud)" in out
    assert "Step 1 of 5: Create" not in out and "Step 3 of 5: Give the Chat API an app name" in out
    assert f"hangouts-chat?project={project}" in out and f"auth/clients?project={project}" in out
    assert "--client-secret" in out


def test_setup_gcloud_personal_account_stops(home, fake, capsys):
    fake.account = "me@gmail.com"
    assert setup.main(["--gcloud", "--non-interactive"]) == 1
    assert "personal Gmail" in capsys.readouterr().out and not any(c[:2] == ["projects", "create"] for c in fake.calls)


def test_guide_uses_the_remembered_project(home, capsys):
    gcloud.PROJECT_FILE.write_text("gchat-mcp-abc123\n")
    assert setup.main(["--guide"]) == 0
    assert "auth/branding?project=gchat-mcp-abc123" in capsys.readouterr().out


def test_setup_gcloud_expired_sign_in_stops_before_any_change(home, fake, capsys):
    fake.expired = True
    assert setup.main(["--gcloud", "--non-interactive"]) == 1
    assert "gcloud auth login" in capsys.readouterr().out
    assert not any(c[:2] in (["projects", "create"], ["services", "enable"]) for c in fake.calls)


def test_setup_gcloud_without_gcloud_installed_prints_all_5_steps(home, monkeypatch, capsys):
    monkeypatch.setattr(gcloud, "available", lambda which=None: False)
    assert setup.main(["--gcloud", "--non-interactive"]) == 1
    out = capsys.readouterr().out
    assert "gcloud is not installed" in out and "Step 1 of 5: Create a Google Cloud project" in out
    assert "Step 5 of 5" in out and "--client-secret" in out


def test_setup_gcloud_signed_out_stops_with_the_login_command(home, fake, capsys):
    fake.account = ""
    assert setup.main(["--gcloud", "--non-interactive"]) == 1
    assert "gcloud auth login" in capsys.readouterr().out and len(fake.calls) == 1


@pytest.mark.parametrize("account, expired, says", [
    ("", False, "not signed in"), ("sam@example.com", True, "could not do steps 1 and 2")])
def test_wizard_falls_back_to_the_browser_when_gcloud_cannot_help(home, fake, monkeypatch, capsys,
                                                                  account, expired, says):
    fake.account, fake.expired = account, expired
    monkeypatch.setattr(setup, "ask", lambda q, d: True)
    args = setup.parser().parse_args([])
    assert setup.cloud(args, interactive=True) == 0
    assert says in capsys.readouterr().out
    assert not any(c[:2] in (["projects", "create"], ["services", "enable"]) for c in fake.calls)
