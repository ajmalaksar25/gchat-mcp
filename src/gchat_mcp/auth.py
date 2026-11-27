"""User OAuth credentials: login subcommand writes the token; the server only reads/refreshes it."""
import os
import time
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

DIR = Path.home() / ".config" / "gchat-mcp"
CLIENT = DIR / "client_secret.json"
TOKEN = DIR / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/chat.users.readstate.readonly",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.messages.reactions.create",
    "https://www.googleapis.com/auth/chat.messages",  # edit_message, delete_message (messages.patch / messages.delete)
    "https://www.googleapis.com/auth/chat.messages.reactions",  # react(remove=True) (reactions.delete)
    "https://www.googleapis.com/auth/chat.users.readstate",  # mark_read, mark_unread (read-state updates)
    "https://www.googleapis.com/auth/chat.spaces.create",  # start_conversation (spaces.setup)
    "https://www.googleapis.com/auth/chat.users.spacesettings",  # notification_settings / set_notifications
    "https://www.googleapis.com/auth/directory.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class AuthError(RuntimeError):
    pass


# Remote mode (`gchat-mcp serve`) installs a source that returns the calling user's Google credentials for the
# current HTTP request; the local token file is then never read.
_remote_source = None
_relogin = "run `gchat-mcp login`"


def set_remote_source(source, relogin: str) -> None:
    global _remote_source, _relogin
    _remote_source, _relogin = source, relogin


def relogin_hint() -> str:
    """The next step that grants missing scopes or a fresh sign-in, for this mode."""
    return _relogin


def _save(creds: Credentials) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(TOKEN, 0o600)


def credentials() -> Credentials:
    """The one source of user credentials: the saved token, refreshed and persisted if expired.

    Never opens a browser. Every Google call gets its credentials here and nowhere else.
    """
    if _remote_source is not None:
        return _remote_source()
    if not TOKEN.exists():
        raise AuthError("Google Chat is not authorised: run `gchat-mcp login` in a terminal.")
    # Keep the token's own granted scopes: refreshing with an ungranted scope fails and would break reads.
    creds = Credentials.from_authorized_user_file(str(TOKEN))
    if not creds.valid:
        if not creds.refresh_token:
            raise AuthError("Saved token has no refresh token: run `gchat-mcp login`.")
        try:
            creds.refresh(Request())
        except GoogleAuthError as e:
            raise AuthError(f"Token refresh failed ({e}): run `gchat-mcp login` if this persists.") from e
        _save(creds)
    return creds


LOGIN_SECONDS = 600


def login() -> None:
    """Open the Google sign-in in a browser and save the token; every failure says what to do next."""
    if not CLIENT.exists():
        raise SystemExit(f"Missing OAuth client file {CLIENT}: run `gchat-mcp setup` to create it.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    start = time.monotonic()
    try:
        # Fixed port: a Web client only accepts a registered redirect (http://localhost:53682/); Desktop accepts any.
        creds = flow.run_local_server(port=53682, prompt="consent", timeout_seconds=LOGIN_SECONDS,
                                      authorization_prompt_message="If no browser opened, open this link: {url}")
    except WSGITimeoutError:  # also raised at once when anything but Google's redirect reaches the port
        if time.monotonic() - start >= LOGIN_SECONDS:
            raise SystemExit("Sign-in was not finished within 10 minutes. Run `gchat-mcp login` and try again.") from None
        raise SystemExit("The sign-in did not come back from Google. Run `gchat-mcp login` again, click Allow on the "
                         "Google page, and wait for 'Saved token'.") from None
    except OSError as e:
        raise SystemExit(f"Could not listen on http://localhost:53682/ ({e}). Close any other `gchat-mcp login` "
                         "window and try again.") from None
    except Exception as e:  # oauthlib raises its own classes for a denied or broken consent
        raise SystemExit(f"Google sign-in failed ({e}). Run `gchat-mcp login` again and click Allow.") from None
    _save(creds)
    print(f"Saved token to {TOKEN}")
