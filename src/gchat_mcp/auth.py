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
    "https://www.googleapis.com/auth/directory.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class AuthError(RuntimeError):
    pass


def _save(creds: Credentials) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(TOKEN, 0o600)


def credentials() -> Credentials:
    """Load the saved token, refreshing and persisting it if expired. Never opens a browser."""
    if not TOKEN.exists():
        raise AuthError("Google Chat is not authorised: run `gchat-mcp login` in a terminal.")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
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
    _save(flow.run_local_server(port=0, prompt="consent"))
    print(f"Saved token to {TOKEN}")
