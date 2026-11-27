"""`gchat-mcp setup`: a guided first run. Google Cloud steps, the key file, sign-in, a check, options, client config.

Every step says what it is, what to click and what success looks like. Every flag also works without a
terminal prompt (`--non-interactive`), so an AI assistant can run the same steps for the user.
"""
import argparse
import getpass
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import webbrowser
from importlib import resources
from pathlib import Path

from . import auth, gcloud

C = "https://console.cloud.google.com"
AVATAR = "https://developers.google.com/chat/images/quickstart-app-avatar.png"
# (title, what it is, link, what to click, what you see when it worked)
GUIDE = [
    ("Create a Google Cloud project",
     "A project is a free folder in Google Cloud that holds this tool's sign-in settings. No billing is needed.",
     f"{C}/projectcreate",
     "Type a name such as gchat-mcp and click Create. Then check that the new project is selected in the "
     "project picker at the top of the page.",
     "The project name shows at the top of the page."),
    ("Turn on the Google Chat API and the People API",
     "This lets programs you sign in to read and send your Chat messages and show people's names.",
     f"{C}/flows/enableapi?apiid=chat.googleapis.com,people.googleapis.com",
     "Check that your new project is selected, then click Next and Enable.",
     "The page says the APIs are enabled."),
    ("Give the Chat API an app name",
     "Google asks for a name, an icon and a description before any program may use Chat, even one only you use.",
     f"{C}/apis/api/chat.googleapis.com/hangouts-chat",
     f"App name: gchat-mcp. Avatar URL: {AVATAR}. Description: My assistant. If you see "
     "'Interactive features', switch it off. Click Save.",
     "A message says the configuration was saved."),
    ("Set up the sign-in screen (OAuth consent screen)",
     "This is the page Google shows when you sign in, where you allow this tool to use your Chat.",
     f"{C}/auth/branding",
     "Click Get started. App name: gchat-mcp. User support email: your email. Audience: Internal (if Internal "
     "is greyed out, choose External, and later add your own email under Audience > Test users). Contact "
     "email: your email. Tick the agreement, click Create.",
     "You see the Google Auth Platform overview page."),
    ("Create and download the key file (OAuth client)",
     "This small file tells Google which program asks for your sign-in. It stays on your computer.",
     f"{C}/auth/clients",
     "Click Create client. Application type: Desktop app. Name: gchat-mcp. Click Create, then Download JSON.",
     "A file named client_secret_....json is in your Downloads folder."),
]
SUBQ = (
    "SubQ (optional, off unless you add a key). SubQ is a separate company's service (api.subq.ai) that sorts new "
    "Chat messages into 'reply needed', 'worth a look' and 'no reply needed'. When it is on, each new message that "
    "may need you is sent to SubQ together with up to 3 earlier messages, the sender names, the conversation title "
    "and your display name. SubQ keeps these inputs for up to 60 days unless your SubQ organisation has zero data "
    "retention. Without a key nothing is sent to SubQ. To turn it off later, delete ~/.config/gchat-mcp/subq_key "
    "or set GCHAT_TRIAGE_SNAP=off."
)
NAME = "gchat"  # the server's name in each client's config
UVX = ["--python", "3.13", "google-chat-mcp"]  # uv does not pick a Python by requires-python; macOS ships 3.9


class Stop(SystemExit):
    """A step that cannot continue: the message says what to do next."""


def prompt_text() -> str:
    return resources.files(__package__).joinpath("agent_prompt.md").read_text()


def clean_path(raw: str) -> Path:
    """A path as typed, pasted or dragged into a terminal: quotes, backslash-escaped spaces and `~` handled."""
    s = raw.strip()
    if len(s) > 1 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    return Path(s.replace("\\ ", " ")).expanduser()


def client_kind(data) -> str:
    """'desktop' or 'web' for an OAuth client file's JSON; raises Stop for anything else."""
    for kind, key in (("desktop", "installed"), ("web", "web")):
        if isinstance(data, dict) and isinstance(data.get(key), dict) and data[key].get("client_id"):
            return kind
    raise Stop("This is not an OAuth client file. Download it again: step 5, " + GUIDE[4][2]
               + " (Create client > Desktop app > Download JSON).")


def newest_download(folder: Path) -> Path | None:
    files = sorted(folder.glob("client_secret*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def client_id(data) -> str:
    return next((data[k]["client_id"] for k in ("installed", "web") if isinstance(data.get(k), dict)), "")


def store_client(src: Path) -> str:
    """Validate and copy the client file to the config dir (0600); returns its kind."""
    try:
        data = json.loads(src.read_text())
    except FileNotFoundError:
        raise Stop(f"No file at {src}. Check the name in your Downloads folder and try again.") from None
    except (OSError, ValueError) as e:
        raise Stop(f"Could not read {src} as JSON ({e}). Download the key file again (step 5).") from None
    kind = client_kind(data)
    if src.resolve() != auth.CLIENT.resolve():
        write_private(auth.CLIENT, json.dumps(data))
    os.chmod(auth.CLIENT, 0o600)
    return kind


def token_client_id() -> str:
    """The OAuth client the saved token belongs to ('' when there is no readable token)."""
    try:
        return json.loads(auth.TOKEN.read_text()).get("client_id", "")
    except (OSError, ValueError):
        return ""


def clipboard_command(system: str, which=shutil.which) -> list[str] | None:
    """The command that copies stdin to the clipboard on this system, or None."""
    options = {"Darwin": [["pbcopy"]], "Windows": [["clip"]],
               "Linux": [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]}
    return next((c for c in options.get(system, []) if which(c[0])), None)


def copy(text: str) -> bool:
    cmd = clipboard_command(platform.system())
    if not cmd:
        return False
    try:
        return subprocess.run(cmd, input=text.encode(), timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def server_command(which=shutil.which, here: Path = Path(__file__).resolve(), prefix: str = sys.prefix) -> list[str]:
    """How a client should start the server, as this setup was run: uvx, a source checkout, or an install.

    Paths are absolute because GUI apps (Claude Desktop, Cursor) do not get the shell's PATH."""
    uvx, uv = which("uvx"), which("uv")
    root = here.parents[2]
    if "archive-v0" in Path(prefix).parts and uvx:
        return [uvx, *UVX]
    if "site-packages" not in here.parts and (root / "pyproject.toml").exists() and uv:
        return [uv, "run", "--directory", str(root), "gchat-mcp"]
    exe = which("gchat-mcp")
    if exe:
        return [str(Path(exe).resolve())]
    return [uvx, *UVX] if uvx else [str(Path(sys.argv[0]).resolve())]


def configs(cmd: list[str], which=shutil.which) -> str:
    """Ready-to-paste config for Claude Code, Codex CLI, Claude Desktop and Cursor."""
    head = Path(cmd[0]).name if which(Path(cmd[0]).name) == cmd[0] else cmd[0]  # short name only if PATH finds it
    short = shlex.join([head, *cmd[1:]])
    entry = json.dumps({"mcpServers": {NAME: {"command": cmd[0], "args": cmd[1:]}}}, indent=2)
    desktop = ("~/Library/Application Support/Claude/claude_desktop_config.json" if platform.system() == "Darwin"
               else "~/.config/Claude/claude_desktop_config.json")
    return "\n".join([
        "Claude Code (run in a terminal):", f"  claude mcp add -s user {NAME} -- {short}", "",
        "Codex CLI (run in a terminal):", f"  codex mcp add {NAME} -- {short}", "",
        f"Claude Desktop: merge into {desktop} (Settings > Developer > Edit Config), then restart it:", entry, "",
        "Cursor: merge into ~/.cursor/mcp.json (Settings > MCP > Add new server), then restart it:", entry])


def explain_api_error(e) -> str:
    """The next step for a failed check call (`chat.ApiError`)."""
    text = f"{e.msg} {e.reason}".lower()
    if e.status == 403 and ("service_disabled" in text or "has not been used" in text or "disabled" in text):
        return f"The Chat or People API is off in your project. Do step 2: {GUIDE[1][2]}"
    if e.status == 404 and "chat app not found" in text:
        return f"The Chat API has no app name yet. Do step 3: {GUIDE[2][2]}"
    if e.status in (401, 403):
        return "Google refused the sign-in. Run `gchat-mcp login` again and click Allow on every permission."
    return f"Google answered HTTP {e.status}: {e.msg}. Try again in a minute; if it repeats, open an issue."


def check() -> str:
    """One read-only call to Google Chat. Returns 'Ready: ...' or raises Stop with the next step."""
    from . import chat
    if not auth.CLIENT.exists():
        raise Stop("Not set up yet: no key file. Run `gchat-mcp setup` (or `setup --guide` for the Google steps).")
    try:
        email = chat.me_email()
        found, more = chat.spaces()
    except auth.AuthError as e:
        raise Stop(f"Not signed in: {e}") from None
    except chat.ApiError as e:
        raise Stop(explain_api_error(e)) from None
    return f"Ready: signed in as {email}; {len(found)}{'+' if more else ''} conversations found."


def ask(question: str, default: bool) -> bool:
    got = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not got else got.startswith("y")


def show(step: int, interactive: bool, project: str = "") -> None:
    title, what, url, click, success = GUIDE[step]
    url = gcloud.with_project(url, project)
    if project and step >= 2:
        click = f"Check that project {project} is selected at the top of the page. " + click
    print(f"\nStep {step + 1} of {len(GUIDE)}: {title}\n  What: {what}\n  Open: {url}\n  Do:   {click}\n"
          f"  Done when: {success}")
    if interactive:
        if input("  Press Enter to open the page, or type 'skip' if you already did this: ").strip() != "skip":
            webbrowser.open(url)
            input("  Press Enter when it worked. ")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gchat-mcp setup", description="Set up Google Chat access, step by step.")
    a = p.add_argument
    a("--guide", action="store_true", help="print the 5 Google Cloud steps and exit")
    a("--check", action="store_true", help="test the sign-in with one read-only call and exit")
    a("--print-config", action="store_true", help="print the config for Claude Code, Codex, Claude Desktop, Cursor")
    a("--agent-prompt", action="store_true", help="print the setup prompt for an AI assistant and copy it")
    a("--explain-subq", action="store_true", help="print what SubQ receives and keeps, and exit")
    a("--gcloud", action="store_true", help="do steps 1-2 (project, APIs) with the gcloud CLI, then show steps 3-5")
    a("--project", metavar="ID", help="with --gcloud or --guide: use this Google Cloud project (default: the one "
                                       "setup made before, else a new one)")
    a("--non-interactive", action="store_true", help="never prompt; use only the flags below")
    a("--client-secret", metavar="PATH", help="the downloaded client_secret_....json")
    a("--skip-login", action="store_true", help="do not open the Google sign-in")
    a("--relogin", action="store_true", help="sign in again even when already signed in")
    a("--subq-key-file", metavar="PATH", help="turn SubQ on with the key stored in this file")
    a("--footer", metavar="TEXT", help="text added under each message you send ('off' removes it)")
    return p


def save_footer(text: str) -> str:
    from . import chat, footer, state
    value = footer.clean(text)
    try:
        me = chat.me_id()
    except auth.AuthError:
        raise Stop("The footer is stored per account, so it needs a sign-in first. Run `gchat-mcp setup` again "
                   "without --skip-login.") from None
    with state.store().transact(me) as st:
        st["settings"]["footer"] = value
    return f"Footer: {value}" if value else "Footer: off"


def cloud(args, interactive: bool) -> int:
    """Steps 1-2 through gcloud when asked (or offered and accepted). Returns the first guide step still to do.

    Without gcloud, or when gcloud is signed out and it was only offered, setup carries on with all 5 steps in the
    browser. An explicit --gcloud that cannot run because of the sign-in stops with the command to fix it."""
    if not args.gcloud and not interactive:
        return 0
    if not gcloud.available():
        if args.gcloud:
            print("\ngcloud is not installed, so all 5 steps are done in the web browser. (Installing the gcloud CLI, "
                  "https://cloud.google.com/sdk/docs/install, would do steps 1 and 2 for you.)")
        return 0
    who = gcloud.account()
    if not args.gcloud:
        if not who:
            print("\nTip: gcloud is installed but not signed in. `gcloud auth login` first would do steps 1 and 2 "
                  "for you; carrying on in the browser.")
            return 0
        if not ask(f"\ngcloud is signed in as {who}. Use it to create the project and turn on the APIs "
                   "(steps 1 and 2)?", True):
            return 0
    try:
        gcloud.check_account(who)
        gcloud.check_token()
        project, created = gcloud.ensure_project(args.project or "")
        print(f"\nStep 1 of {len(GUIDE)}: project {project} {'created' if created else 'in use'} (gcloud).")
        turned_on = gcloud.enable_apis(project)
    except gcloud.Failed as e:
        if args.gcloud:
            raise
        print(f"\ngcloud could not do steps 1 and 2: {e.code}\nCarrying on with all 5 steps in the browser.")
        return 0
    print(f"Step 2 of {len(GUIDE)}: {', '.join(turned_on) or 'Chat and People APIs'} "
          f"{'turned on' if turned_on else 'already on'} (gcloud).")
    args.project = project
    return 2


def get_client(args, interactive: bool, first: int = 0) -> None:
    if args.client_secret:
        src = clean_path(args.client_secret)
    elif auth.CLIENT.exists():
        print(f"\nKey file: already in place ({auth.CLIENT}).")
        return
    elif not interactive:
        if first or args.gcloud:
            print(f"\nSteps {first + 1}-{len(GUIDE)}" + (" have no gcloud command;" if first else ":")
                  + " do them in your web browser:")
            for i in range(first, len(GUIDE)):
                show(i, False, args.project or "")
        raise Stop("No key file yet. Do the Google Cloud steps (`gchat-mcp setup --guide`), then run again with "
                   "--client-secret <path to the downloaded client_secret_....json>.")
    else:
        if not first:
            print("\nThis tool needs a Google Workspace account (work or school, not @gmail.com) and about 10 "
                  "minutes.")
        print(f"\n{len(GUIDE) - first} step(s) in Google Cloud, in your web browser:")
        for i in range(first, len(GUIDE)):
            show(i, interactive, args.project or "")
        found = newest_download(Path.home() / "Downloads")
        if found and ask(f"\nUse the key file {found}?", True):
            src = found
        else:
            src = clean_path(input("Drag the downloaded client_secret_....json file here (or type its path), "
                                   "then press Enter: "))
    kind = store_client(src)
    print(f"Key file: saved to {auth.CLIENT} (only you can read it).")
    old = token_client_id()
    if old and old != client_id(json.loads(auth.CLIENT.read_text())):
        args.relogin = True  # the saved sign-in belongs to another client
        print("  This is a different key file from your last sign-in, so you will sign in again.")
    if kind == "web":
        print("  Note: this is a Web client. It works only if http://localhost:53682/ is one of its authorised "
              "redirect URIs; a Desktop app client needs nothing extra.")


def sign_in(args, interactive: bool) -> None:
    if args.skip_login:
        return
    if auth.TOKEN.exists() and not args.relogin:
        try:
            auth.credentials()
            print("Sign-in: already signed in (use --relogin to sign in again).")
            return
        except auth.AuthError:
            pass
    print("\nSign-in: a Google page opens in your browser. Choose your work account and click Allow.\n"
          "  If Google says the app is not verified, click Continue: it is your own app.")
    if interactive:
        input("  Press Enter to open it. ")
    auth.login()


def options(args, interactive: bool) -> None:
    if args.subq_key_file:
        try:
            key = clean_path(args.subq_key_file).read_text().strip()
        except OSError as e:
            raise Stop(f"Could not read the SubQ key file ({e}). Save the key in a file and give its path.") from None
    elif interactive and not auth_key_present():
        print("\n" + SUBQ)
        key = getpass.getpass("Paste a SubQ key to turn it on (hidden), or press Enter to keep it off: ").strip()
    else:
        key = ""
    if key:
        from . import snap
        write_private(snap.KEY_FILE, key + "\n")
        print(f"SubQ: on (key saved to {snap.KEY_FILE}).")
    if args.footer is not None:
        print(save_footer(args.footer))
    elif interactive and ask("\nAdd a line under each message you send, so people know your assistant wrote it?",
                             False):
        text = input("Footer text ({user} becomes your name) [_Sent by my AI assistant_]: ").strip()
        print(save_footer(text or "_Sent by my AI assistant_"))


def auth_key_present() -> bool:
    from . import snap
    return snap.key() is not None


def prog(prefix: str = sys.prefix) -> str:
    """How the user runs this tool: through uvx when this is a uvx environment, else the installed command."""
    return "uvx " + " ".join(UVX) if "archive-v0" in Path(prefix).parts else "gchat-mcp"


def named(text: str) -> str:
    return text.replace("`gchat-mcp ", f"`{prog()} ")


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # keep progress and errors in order when output is piped
    args = parser().parse_args(argv)
    if args.agent_prompt:
        text = prompt_text()
        print(text)
        print("Copied to your clipboard." if copy(text) else "Copy the text above.",
              "Paste it into your AI assistant (Claude Code, Claude Desktop, Cursor, Codex...) and send it.",
              file=sys.stderr)
        return 0
    if args.explain_subq:
        print(SUBQ)
        return 0
    if args.guide:
        project = args.project or gcloud.remembered()
        for i in range(len(GUIDE)):
            show(i, False, project)
        print(named("\nThen run: `gchat-mcp setup --client-secret <path to the downloaded file>`"))
        return 0
    if args.print_config:
        print(configs(server_command()))
        return 0
    try:
        if not args.check:
            interactive = not args.non_interactive and sys.stdin.isatty()
            first = cloud(args, interactive) if not args.client_secret and not auth.CLIENT.exists() else 0
            get_client(args, interactive, first)
            sign_in(args, interactive)
            if args.skip_login and args.relogin and args.footer is not None:
                raise Stop("The new key file needs a new sign-in before the footer can be saved. Run "
                           "`gchat-mcp setup` again without --skip-login.")
            if not args.skip_login or args.footer is not None or args.subq_key_file:
                options(args, interactive)
        print("\n" + check())
    except SystemExit as e:  # Stop, or a sign-in failure from auth.login
        print(named(f"\nNot ready: {e.code}"))  # stdout, so it follows the progress lines in a captured log
        return 1
    if not args.check:
        print("\nLast step: add the server to your AI assistant.\n\n" + configs(server_command())
              + "\n\nThen restart the assistant and ask it: What needs a reply in Google Chat?")
    return 0
