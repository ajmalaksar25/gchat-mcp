"""User OAuth credentials: login subcommand writes the token; the server only reads/refreshes it."""
import os
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

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
    "https://www.googleapis.com/auth/directory.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


_provider = None


class AuthError(RuntimeError):
    pass


def set_credentials_provider(fn) -> None:
    """Replace where credentials come from (a hosted deployment supplies per-request user credentials)."""
    global _provider
    _provider = fn


def _save(creds: Credentials) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(TOKEN, 0o600)


def credentials() -> Credentials:
    """The one source of user credentials. Default: the saved token, refreshed and persisted if expired.

    Never opens a browser.
    """
    if _provider:
        return _provider()
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


def login() -> None:
    if not CLIENT.exists():
        raise SystemExit(f"Missing OAuth client file: {CLIENT}")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    # Fixed port: the shared Web client only accepts the registered http://localhost:53682/.
    _save(flow.run_local_server(port=53682, prompt="consent"))
    print(f"Saved token to {TOKEN}")
