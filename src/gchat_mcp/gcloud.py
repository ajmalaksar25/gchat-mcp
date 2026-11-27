"""Google Cloud steps through the gcloud CLI, when it is installed and signed in.

gcloud does two of the five Google Cloud steps: it creates the project and turns on the Chat and People APIs. The
other three (the Chat app name, the sign-in screen and the OAuth client) have no gcloud command, so setup prints
their console links with the project already selected, which avoids the most common mistake: doing a step in
the wrong project. The project id is remembered, so running setup again reuses it.
"""
import json
import re
import secrets
import shutil
import subprocess

from . import auth

APIS = ("chat.googleapis.com", "people.googleapis.com")
PROJECT_FILE = auth.DIR / "gcloud_project"
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
PERSONAL = ("gmail.com", "googlemail.com")


class Failed(SystemExit):
    """A gcloud step failed; the message says what to do instead."""


def run(args: list[str], timeout: int = 300) -> str:
    """stdout of `gcloud <args>`; Failed with gcloud's own error text otherwise."""
    exe = shutil.which("gcloud")
    if not exe:
        raise Failed("gcloud is not installed.")
    try:
        r = subprocess.run([exe, *args, "--quiet"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Failed(f"`gcloud {' '.join(args[:3])}` did not finish in {timeout} s.") from None
    if r.returncode:
        msg = " ".join((r.stderr or r.stdout).split())[:400]
        raise Failed(f"`gcloud {' '.join(args[:3])}` failed: {msg}")
    return r.stdout.strip()


def available(which=shutil.which) -> bool:
    return which("gcloud") is not None


def account(gc=None) -> str:
    """The signed-in gcloud account ('' when none)."""
    gc = gc or run
    try:
        return gc(["auth", "list", "--filter=status:ACTIVE", "--format=value(account)"]).splitlines()[0].strip()
    except (Failed, IndexError):
        return ""


def check_account(email: str) -> None:
    if not email:
        raise Failed("gcloud is not signed in. Run `gcloud auth login` and choose your work account, then try again.")
    if email.rsplit("@", 1)[-1].lower() in PERSONAL:
        raise Failed(f"gcloud is signed in as {email}, a personal Gmail account. Google Chat's API serves only "
                     "Workspace (work or school) accounts: run `gcloud auth login` with your work account.")


def check_token(gc=None) -> None:
    """Failed when gcloud's saved sign-in can no longer get a token (it then cannot prompt from a script)."""
    gc = gc or run
    try:
        gc(["auth", "print-access-token"], timeout=60)
    except Failed:
        raise Failed("gcloud's sign-in has expired. Run `gcloud auth login` with your work account, then try "
                     "again.") from None


def remembered() -> str:
    try:
        p = PROJECT_FILE.read_text().strip()
        return p if PROJECT_RE.match(p) else ""
    except OSError:
        return ""


def new_id() -> str:
    return f"gchat-mcp-{secrets.token_hex(3)}"


def exists(project: str, gc=None) -> bool:
    gc = gc or run
    try:
        return bool(gc(["projects", "describe", project, "--format=value(projectId)"], timeout=60))
    except Failed as e:
        if "auth" in str(e.code).lower() and "token" in str(e.code).lower():
            raise
        return False


def ensure_project(project: str = "", gc=None) -> tuple[str, bool]:
    """(project id, created now). Uses `project`, else the remembered one, else creates a new project."""
    gc = gc or run
    saved = "" if project else remembered()
    if saved and not exists(saved, gc):  # deleted, or another gcloud account: forget it and make a new one
        print(f"The project setup used before ({saved}) is gone or this gcloud account cannot see it; "
              "creating a new one.")
        PROJECT_FILE.unlink(missing_ok=True)
        saved = ""
    project = project or saved
    if project:
        if not PROJECT_RE.match(project):
            raise Failed(f"{project!r} is not a Google Cloud project id (6-30 lowercase letters, digits, hyphens).")
        if not exists(project, gc):
            raise Failed(f"Project {project} does not exist or this gcloud account cannot see it. Check the id with "
                         "`gcloud projects list`, or leave out --project to use a new one.")
        created = False
    else:
        project = new_id()
        try:
            gc(["projects", "create", project, "--name=gchat-mcp"])
        except Failed as e:
            raise Failed(f"{e.code} Your organisation may not allow new projects: ask your admin for one, or pick "
                         "an existing project with --project ID.") from None
        created = True
    auth.DIR.mkdir(parents=True, exist_ok=True)
    PROJECT_FILE.write_text(project + "\n")
    return project, created


def enabled(project: str, gc=None) -> set[str]:
    gc = gc or run
    out = gc(["services", "list", "--enabled", f"--project={project}", "--format=json(config.name)"], timeout=120)
    return {s.get("config", {}).get("name", "") for s in json.loads(out or "[]")}


def enable_apis(project: str, gc=None) -> list[str]:
    """Turn on the APIs that are still off; returns the ones turned on now."""
    gc = gc or run
    missing = [a for a in APIS if a not in enabled(project, gc)]
    if missing:
        gc(["services", "enable", *missing, f"--project={project}"])
    return missing


def with_project(url: str, project: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}project={project}" if project else url
