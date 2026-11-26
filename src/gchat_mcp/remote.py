"""Remote mode: the same tools over Streamable HTTP, for clients that connect by URL (Claude custom connectors).

The server is its own OAuth 2.1 authorization server for the MCP client (dynamic client registration, PKCE) and
sends each user on to Google's consent page. Google's tokens never live in a database: every value this server
hands out (client id, authorization code, access token, refresh token, the `state` sent to Google) is an
encrypted, authenticated blob (Fernet, key from GCHAT_MCP_SECRET). So the process keeps no per-user data on disk
and any instance with the same secret can serve any request (Cloud Run, Vercel, Fly, a VM).

Per request, the bearer token is decrypted and becomes that user's Google credentials through
`auth.credentials()`, the one source of credentials for every Google call (see `auth.set_remote_source`).

Configuration (environment):
  GCHAT_MCP_BASE_URL              public https origin of this server, e.g. https://gchat-mcp-xyz.a.run.app
  GCHAT_MCP_GOOGLE_CLIENT_ID      OAuth client of type "Web application" in your Google Cloud project
  GCHAT_MCP_GOOGLE_CLIENT_SECRET  its secret
  GCHAT_MCP_SECRET                at least 32 random characters; comma-separate several to rotate (first encrypts)
  GCHAT_MCP_ALLOWED_USERS         who may sign in: emails and/or @domains, comma-separated (required)
  GCHAT_MCP_REDIRECT_URIS         extra exact client redirect URIs to accept (Claude's and loopback are built in)
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import anyio
import requests
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from google.oauth2.credentials import Credentials
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import auth

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
CALLBACK_PATH = "/oauth/google/callback"
MCP_SCOPE = "gchat"
STATE_TTL = 15 * 60  # the user has this long on Google's consent page
CODE_TTL = 120  # authorization code lifetime (single use per instance, PKCE-bound everywhere)
ACCESS_TTL = 3600
REFRESH_TTL = 90 * 24 * 3600  # Claude refreshes long before; an unused connection signs in again after 90 days
BUILTIN_REDIRECTS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    base_url: str
    google_client_id: str
    google_client_secret: str
    secrets: tuple[str, ...]
    allowed: tuple[str, ...]
    redirect_uris: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        need = {k: env.get(k, "").strip() for k in ("GCHAT_MCP_BASE_URL", "GCHAT_MCP_GOOGLE_CLIENT_ID",
                                                   "GCHAT_MCP_GOOGLE_CLIENT_SECRET", "GCHAT_MCP_SECRET",
                                                   "GCHAT_MCP_ALLOWED_USERS")}
        missing = [k for k, v in need.items() if not v]
        if missing:
            raise ConfigError("Remote mode needs these environment variables: " + ", ".join(missing)
                              + ". See docs/remote.md.")
        base = need["GCHAT_MCP_BASE_URL"].rstrip("/")
        u = urlsplit(base)
        if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
            raise ConfigError("GCHAT_MCP_BASE_URL must be an https URL (http only for localhost testing).")
        if u.path or u.query:
            raise ConfigError("GCHAT_MCP_BASE_URL must be an origin with no path, e.g. https://chat-mcp.example.com")
        keys = tuple(s.strip() for s in need["GCHAT_MCP_SECRET"].split(",") if s.strip())
        if any(len(k) < 32 for k in keys):
            raise ConfigError("Each GCHAT_MCP_SECRET value must be at least 32 characters "
                              "(make one with: python3 -c 'import secrets; print(secrets.token_urlsafe(48))').")
        allowed = tuple(a.strip().lower() for a in need["GCHAT_MCP_ALLOWED_USERS"].split(",") if a.strip())
        extra = tuple(r.strip() for r in env.get("GCHAT_MCP_REDIRECT_URIS", "").split(",") if r.strip())
        return cls(base, need["GCHAT_MCP_GOOGLE_CLIENT_ID"], need["GCHAT_MCP_GOOGLE_CLIENT_SECRET"], keys,
                   allowed, extra)

    def user_allowed(self, email: str) -> bool:
        email = (email or "").lower()
        if "@" not in email:
            return False
        domain = "@" + email.rsplit("@", 1)[1]
        return email in self.allowed or domain in self.allowed

    def redirect_allowed(self, uri: str) -> bool:
        if uri in BUILTIN_REDIRECTS or uri in self.redirect_uris:
            return True
        u = urlsplit(uri)  # loopback redirects (Claude Code, Claude Desktop, MCP Inspector) on any port
        return u.scheme == "http" and u.hostname in LOOPBACK_HOSTS


class Box:
    """Encrypt/decrypt typed JSON payloads. `typ` is checked so a refresh token can never pass as an access token."""

    def __init__(self, keys: tuple[str, ...]):
        def derive(k: str) -> Fernet:
            raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"gchat-mcp", info=b"remote-tokens v1").derive(
                k.encode())
            return Fernet(base64.urlsafe_b64encode(raw))
        self._f = MultiFernet([derive(k) for k in keys])
        self._mac = hashlib.sha256(b"gchat-mcp client secret\0" + keys[0].encode()).digest()

    def seal(self, typ: str, data: dict) -> str:
        return self._f.encrypt(json.dumps({"typ": typ, **data}, separators=(",", ":")).encode()).decode()

    def open(self, typ: str, token: str) -> dict | None:
        try:
            d = json.loads(self._f.decrypt(token.encode()))
        except (InvalidToken, ValueError, TypeError):
            return None
        if d.get("typ") != typ or ("exp" in d and d["exp"] < time.time()):
            return None
        return d

    def client_secret(self, client_id: str) -> str:
        """Derived, never stored. Rotating the first key changes it, so confidential clients reconnect then."""
        return hmac.new(self._mac, client_id.encode(), hashlib.sha256).hexdigest()


def _cid(client_id: str) -> str:
    return hashlib.sha256(client_id.encode()).hexdigest()[:24]


class Code(AuthorizationCode):
    google: dict


class Refresh(RefreshToken):
    google: dict


class Access(AccessToken):
    google: dict


def _id_claims(id_token: str, client_id: str) -> dict:
    """Claims of an ID token received straight from Google's token endpoint over TLS (OIDC Core 3.1.3.7 allows
    skipping the signature check for that channel); audience, issuer and expiry are still checked."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise TokenError("invalid_grant", "Google returned an unreadable ID token") from None
    if claims.get("aud") != client_id or claims.get("iss") not in GOOGLE_ISSUERS or claims.get("exp", 0) < time.time():
        raise TokenError("invalid_grant", "Google ID token failed its audience, issuer or expiry check")
    return claims


class GoogleProxyProvider:
    """OAuthAuthorizationServerProvider that keeps no state: everything it issues is a sealed `Box` payload."""

    def __init__(self, cfg: Config, http=requests):
        self.cfg, self.box, self.http = cfg, Box(cfg.secrets), http
        self._used: dict[str, float] = {}  # code hash -> expiry: single use within this instance
        self._lock = threading.Lock()

    # --- clients (RFC 7591): the client id is the sealed registration
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        d = self.box.open("client", client_id)
        if not d:
            return None
        secret = self.box.client_secret(client_id) if d["m"] != "none" else None
        return OAuthClientInformationFull(client_id=client_id, client_secret=secret, redirect_uris=d["r"],
                                          token_endpoint_auth_method=d["m"], client_name=d.get("n"),
                                          scope=d.get("s"), client_id_issued_at=d.get("iat"),
                                          grant_types=["authorization_code", "refresh_token"],
                                          response_types=["code"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        bad = [u for u in uris if not self.cfg.redirect_allowed(u)]
        if not uris or bad:
            raise RegistrationError("invalid_redirect_uri",
                                    f"redirect URI not accepted by this server: {', '.join(bad) or '(none)'}; "
                                    "add it to GCHAT_MCP_REDIRECT_URIS")
        # The SDK hands us the record it will return: replace its random id and secret with stateless ones.
        client_info.client_id = self.box.seal("client", {"r": uris, "m": client_info.token_endpoint_auth_method,
                                                         "n": client_info.client_name, "s": client_info.scope,
                                                         "iat": client_info.client_id_issued_at})
        if client_info.client_secret is not None:
            client_info.client_secret = self.box.client_secret(client_info.client_id)

    # --- authorize: send the user to Google with everything needed to come back sealed into `state`
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = self.box.seal("state", {
            "exp": time.time() + STATE_TTL, "cid": client.client_id, "ru": str(params.redirect_uri),
            "rux": params.redirect_uri_provided_explicitly, "cc": params.code_challenge, "st": params.state,
            "sc": params.scopes or [MCP_SCOPE], "res": params.resource, "v": verifier})
        q = {"client_id": self.cfg.google_client_id, "redirect_uri": self.cfg.base_url + CALLBACK_PATH,
             "response_type": "code", "scope": " ".join(auth.SCOPES), "access_type": "offline",
             "prompt": "consent", "include_granted_scopes": "false", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        domains = [a[1:] for a in self.cfg.allowed if a.startswith("@")]
        if len(domains) == 1 and len(self.cfg.allowed) == 1:
            q["hd"] = domains[0]  # only a hint for the account chooser; the callback enforces the allowlist
        return f"{GOOGLE_AUTH}?{urlencode(q)}"

    def _google_token(self, data: dict) -> dict:
        r = self.http.post(GOOGLE_TOKEN, data={**data, "client_id": self.cfg.google_client_id,
                                                "client_secret": self.cfg.google_client_secret}, timeout=15)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200:
            err = body.get("error", f"HTTP {r.status_code}")
            raise TokenError("invalid_grant", f"Google refused the token request ({err})")
        return body

    async def google_callback(self, request: Request) -> Response:
        """Google redirects here; exchange its code and redirect to the MCP client with our own code."""
        st = self.box.open("state", request.query_params.get("state", ""))
        if not st:
            return PlainTextResponse("This sign-in link expired or is not valid. Start the connection again.", 400)

        def back(**params) -> Response:
            return RedirectResponse(construct_redirect_uri(st["ru"], state=st["st"], **params), 302)

        if request.query_params.get("error"):
            return back(error="access_denied", error_description="Google sign-in was cancelled or refused")
        code = request.query_params.get("code", "")
        try:
            tok = await anyio.to_thread.run_sync(lambda: self._google_token({
                "grant_type": "authorization_code", "code": code, "code_verifier": st["v"],
                "redirect_uri": self.cfg.base_url + CALLBACK_PATH}))
            claims = _id_claims(tok.get("id_token", ""), self.cfg.google_client_id)
        except TokenError as e:
            return back(error="server_error", error_description=e.error_description or "Google sign-in failed")
        email = claims.get("email", "")
        if not claims.get("email_verified") or not self.cfg.user_allowed(email):
            return back(error="access_denied", error_description=f"{email or 'This account'} may not use this server")
        if not tok.get("refresh_token"):
            return back(error="server_error", error_description="Google returned no refresh token; try again")
        granted = set(tok.get("scope", "").split())
        g = {"rt": tok["refresh_token"], "at": tok.get("access_token"),
             "ax": time.time() + int(tok.get("expires_in", 0)) - 60, "sub": claims["sub"], "email": email,
             "scopes": sorted(granted) if granted else auth.SCOPES}
        ours = self.box.seal("code", {"exp": time.time() + CODE_TTL, "cid": _cid(st["cid"]), "ru": st["ru"],
                                      "rux": st["rux"], "cc": st["cc"], "sc": st["sc"], "res": st["res"], "g": g})
        return back(code=ours)

    # --- tokens
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        d = self.box.open("code", authorization_code)
        if not d or d["cid"] != _cid(client.client_id):
            return None
        h = hashlib.sha256(authorization_code.encode()).hexdigest()
        with self._lock:
            now = time.time()
            for k in [k for k, exp in self._used.items() if exp < now]:
                del self._used[k]
            if h in self._used:
                return None
            self._used[h] = d["exp"]
        return Code(code=authorization_code, scopes=d["sc"], expires_at=d["exp"], client_id=client.client_id,
                    code_challenge=d["cc"], redirect_uri=d["ru"], redirect_uri_provided_explicitly=d["rux"],
                    resource=d["res"], subject=d["g"]["sub"], google=d["g"])

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, g: dict) -> OAuthToken:
        now = time.time()
        base = {"cid": _cid(client_id), "sc": scopes, "res": resource}
        access = self.box.seal("access", {**base, "exp": now + ACCESS_TTL, "g": g})
        refresh = self.box.seal("refresh", {**base, "exp": now + REFRESH_TTL,
                                            "g": {k: g[k] for k in ("rt", "sub", "email", "scopes")}})
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, scope=" ".join(scopes),
                          refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: Code):
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource,
                           authorization_code.google)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        d = self.box.open("refresh", refresh_token)
        if not d or d["cid"] != _cid(client.client_id):
            return None
        return Refresh(token=refresh_token, client_id=client.client_id, scopes=d["sc"], expires_at=int(d["exp"]),
                       resource=d["res"], subject=d["g"]["sub"], google=d["g"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: Refresh,
                                     scopes: list[str]) -> OAuthToken:
        g = dict(refresh_token.google)
        if not self.cfg.user_allowed(g.get("email", "")):
            raise TokenError("invalid_grant", "this account may no longer use this server")
        # Refreshing at Google here proves the grant is still alive (the user can revoke it in their Google account).
        tok = await anyio.to_thread.run_sync(lambda: self._google_token({"grant_type": "refresh_token",
                                                                        "refresh_token": g["rt"]}))
        g.update(at=tok.get("access_token"), ax=time.time() + int(tok.get("expires_in", 0)) - 60)
        if tok.get("refresh_token"):
            g["rt"] = tok["refresh_token"]
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource, g)

    async def load_access_token(self, token: str) -> Access | None:
        d = self.box.open("access", token)
        if not d or not self.cfg.user_allowed(d["g"].get("email", "")):
            return None
        if d["res"] and d["res"].rstrip("/") not in (self.cfg.base_url, self.cfg.base_url + "/mcp"):
            return None  # issued for another resource
        return Access(token=token, client_id=d["cid"], scopes=d["sc"], expires_at=int(d["exp"]), resource=d["res"],
                      subject=d["g"]["sub"], google=d["g"])

    async def revoke_token(self, token) -> None:
        rt = getattr(token, "google", {}).get("rt")
        if rt:  # revoking the Google grant ends every token issued from it
            await anyio.to_thread.run_sync(lambda: self.http.post(GOOGLE_REVOKE, data={"token": rt}, timeout=15))


# --- Google credentials for the current request
_creds: dict[str, Credentials] = {}
_creds_lock = threading.Lock()


def _request_credentials(cfg: Config):
    def source() -> Credentials:
        tok = get_access_token()
        g = getattr(tok, "google", None)
        if not g:
            raise auth.AuthError("Not signed in to Google Chat: reconnect the connector.")
        key = hashlib.sha256(g["rt"].encode()).hexdigest()
        with _creds_lock:
            creds = _creds.get(key)
            if creds is None:
                from datetime import datetime, timezone
                expiry = datetime.fromtimestamp(g.get("ax") or 0, timezone.utc).replace(tzinfo=None)
                creds = Credentials(token=g.get("at"), refresh_token=g["rt"], token_uri=GOOGLE_TOKEN,
                                    client_id=cfg.google_client_id, client_secret=cfg.google_client_secret,
                                    scopes=g.get("scopes"), expiry=expiry)
                _creds[key] = creds
        return creds  # AuthorizedSession refreshes it in place when it expires
    return source


def configure(mcp, cfg: Config, http=requests) -> GoogleProxyProvider:
    """Turn the stdio FastMCP instance into an authenticated Streamable HTTP server."""
    provider = GoogleProxyProvider(cfg, http)
    mcp.settings.auth = AuthSettings(
        issuer_url=AnyHttpUrl(cfg.base_url), resource_server_url=AnyHttpUrl(cfg.base_url + "/mcp"),
        validate_token_resource=False,  # load_access_token checks the resource itself (it may be absent)
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[MCP_SCOPE],
                                                              default_scopes=[MCP_SCOPE]),
        revocation_options=RevocationOptions(enabled=True), required_scopes=[MCP_SCOPE])
    mcp._auth_server_provider = provider
    from mcp.server.auth.provider import ProviderTokenVerifier
    mcp._token_verifier = ProviderTokenVerifier(provider)
    mcp.settings.stateless_http = True  # any instance serves any request; nothing per session in memory
    host = urlsplit(cfg.base_url).netloc
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=[host],
        allowed_origins=[cfg.base_url, "https://claude.ai", "https://claude.com"])
    mcp.custom_route(CALLBACK_PATH, methods=["GET"])(provider.google_callback)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")

    auth.set_remote_source(_request_credentials(cfg), "disconnect and reconnect the Google Chat connector")
    return provider
