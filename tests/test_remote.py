"""Remote mode: the whole OAuth dance (registration, Google round trip, tokens) and per-request credentials."""
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gchat_mcp import auth, remote

BASE = "https://gchat.example.com"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
ENV = {"GCHAT_MCP_BASE_URL": BASE, "GCHAT_MCP_GOOGLE_CLIENT_ID": "gid.apps.googleusercontent.com",
       "GCHAT_MCP_GOOGLE_CLIENT_SECRET": "gsecret", "GCHAT_MCP_SECRET": "k" * 40,
       "GCHAT_MCP_ALLOWED_USERS": "@example.com,guest@other.org"}


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeGoogle:
    """Google's token and revoke endpoints; `email` is who signs in on the consent page."""

    def __init__(self):
        self.email, self.calls, self.refresh_ok, self.n = "ajmal@example.com", [], True, 0

    def post(self, url, data, timeout):
        self.calls.append((url, data))
        self.n += 1
        if url == remote.GOOGLE_REVOKE:
            return FakeResp(200, {})
        if data["grant_type"] == "authorization_code":
            assert data["client_secret"] == "gsecret" and data["code_verifier"]
            claims = {"aud": ENV["GCHAT_MCP_GOOGLE_CLIENT_ID"], "iss": "https://accounts.google.com",
                      "exp": time.time() + 600, "sub": f"sub-{self.email}", "email": self.email,
                      "email_verified": True}
            return FakeResp(200, {"access_token": f"gat-{self.email}-{self.n}", "expires_in": 3599,
                                  "refresh_token": f"grt-{self.email}", "id_token": _jwt(claims),
                                  "scope": " ".join(auth.SCOPES)})
        if not self.refresh_ok:
            return FakeResp(400, {"error": "invalid_grant"})
        return FakeResp(200, {"access_token": f"gat-refreshed-{self.n}", "expires_in": 3599})


def _app(g, guard=None):
    """One server instance (a replica) on its own FastMCP; replicas share only the secret and the guard."""
    mcp = FastMCP("t")

    @mcp.tool()
    async def whoami() -> str:
        """Returns the Google access token this request would use (checks the context reaches threads)."""
        return await anyio.to_thread.run_sync(lambda: auth.credentials().token)

    remote.configure(mcp, remote.Config.from_env(ENV), http=g, guard=guard)
    app = mcp.streamable_http_app
    return TestClient(app if isinstance(app, Starlette) else app(), base_url=BASE, follow_redirects=False)


@pytest.fixture
def server(monkeypatch):
    g = FakeGoogle()
    remote._creds.clear()
    with _app(g) as client:
        yield client, g
    auth.set_remote_source(None, "run `gchat-mcp login`")


class FakeFirestore:
    """createDocument: 200 the first time a document id is used, 409 ALREADY_EXISTS after that."""

    def __init__(self):
        self.docs, self.down = {}, False

    def post(self, url, params, json, timeout):
        assert url.endswith("/documents/gchat_mcp_spent") and "expireAt" in json["fields"]
        if self.down:
            return FakeResp(503, {})
        if params["documentId"] in self.docs:
            return FakeResp(409, {"error": {"status": "ALREADY_EXISTS"}})
        self.docs[params["documentId"]] = json
        return FakeResp(200, {})


@pytest.fixture
def replicas():
    """Two instances behind one URL, sharing a Firestore guard, as on Cloud Run with several instances."""
    g, fs = FakeGoogle(), FakeFirestore()
    guard = remote.FirestoreGuard("proj", session=fs)
    remote._creds.clear()
    with _app(g, guard) as a, _app(g, guard) as b:
        yield a, b, g, fs
    auth.set_remote_source(None, "run `gchat-mcp login`")


def _pkce():
    v = secrets.token_urlsafe(40)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


def _register(c, redirect=CLAUDE_CB, method="none"):
    return c.post("/register", json={"redirect_uris": [redirect], "token_endpoint_auth_method": method,
                                     "grant_types": ["authorization_code", "refresh_token"],
                                     "response_types": ["code"], "client_name": "Claude"})


def _sign_in(c, g, email="ajmal@example.com"):
    """Run the browser part: returns (client_id, our code, verifier) or the final redirect's query on error."""
    reg = _register(c).json()
    v, ch = _pkce()
    r = c.get("/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": CLAUDE_CB,
                                    "code_challenge": ch, "code_challenge_method": "S256", "state": "claude-state",
                                    "resource": BASE + "/mcp"})
    assert r.status_code == 302 and r.headers["location"].startswith(remote.GOOGLE_AUTH)
    gq = parse_qs(urlsplit(r.headers["location"]).query)
    assert gq["redirect_uri"] == [BASE + remote.CALLBACK_PATH] and gq["access_type"] == ["offline"]
    assert set(gq["scope"][0].split()) == set(auth.SCOPES) and gq["code_challenge_method"] == ["S256"]
    g.email = email
    back = c.get(remote.CALLBACK_PATH, params={"code": "google-code", "state": gq["state"][0]})
    assert back.status_code == 302 and back.headers["location"].startswith(CLAUDE_CB)
    q = parse_qs(urlsplit(back.headers["location"]).query)
    assert q["state"] == ["claude-state"]
    return reg["client_id"], q, v


def _token(c, client_id, code, verifier):
    return c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CB,
                                  "client_id": client_id, "code_verifier": verifier})


def _rpc(c, token, method, params=None, id_=1):
    r = c.post("/mcp", headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                                "MCP-Protocol-Version": "2025-06-18"},
               json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    if r.status_code != 200:
        return r.status_code, None
    body = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    return 200, json.loads(body)


def _whoami(c, token):
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    assert _rpc(c, token, "initialize", init)[0] == 200
    status, out = _rpc(c, token, "tools/call", {"name": "whoami", "arguments": {}}, 2)
    return status, out and out["result"]["content"][0]["text"]


def test_discovery_and_unauthenticated(server):
    c, _ = server
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"] == BASE + "/register" and "S256" in meta["code_challenge_methods_supported"]
    prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == BASE + "/mcp" and prm["authorization_servers"] == [BASE + "/"]
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    assert c.get("/healthz").text == "ok"


def test_full_flow_then_tool_call_uses_that_users_google_token(server):
    c, g = server
    cid, q, v = _sign_in(c, g)
    tok = _token(c, cid, q["code"][0], v)
    assert tok.status_code == 200, tok.text
    t = tok.json()
    assert t["token_type"].lower() == "bearer" and t["refresh_token"] and t["scope"] == "gchat"
    assert "grt-" not in t["access_token"]  # sealed, not readable
    assert _whoami(c, t["access_token"]) == (200, "gat-ajmal@example.com-1")
    # the code is single use
    assert _token(c, cid, q["code"][0], v).status_code == 400


def test_two_users_never_share_credentials(server):
    c, g = server
    a = _sign_in(c, g, "ajmal@example.com")
    ta = _token(c, *[a[0], a[1]["code"][0], a[2]]).json()["access_token"]
    b = _sign_in(c, g, "guest@other.org")
    tb = _token(c, *[b[0], b[1]["code"][0], b[2]]).json()["access_token"]
    assert _whoami(c, ta)[1].startswith("gat-ajmal@example.com")
    assert _whoami(c, tb)[1].startswith("gat-guest@other.org")


def test_wrong_verifier_and_tampered_tokens_are_refused(server):
    c, g = server
    cid, q, v = _sign_in(c, g)
    assert _token(c, cid, q["code"][0], "x" * 50).status_code == 400
    cid, q, v = _sign_in(c, g)
    t = _token(c, cid, q["code"][0], v).json()
    assert _rpc(c, t["access_token"][:-4] + "AAAA", "tools/list")[0] == 401
    assert _rpc(c, t["refresh_token"], "tools/list")[0] == 401  # a refresh token is not an access token


def test_user_outside_allowlist_is_refused_at_google_callback(server):
    c, g = server
    _, q, _ = _sign_in(c, g, "stranger@gmail.com")
    assert q["error"] == ["access_denied"] and "code" not in q


def test_registration_refuses_unknown_redirects_and_accepts_loopback(server):
    c, _ = server
    assert _register(c, "https://evil.example/cb").status_code == 400
    assert _register(c, "http://localhost:33418/callback").status_code == 201
    conf = _register(c, method="client_secret_post").json()
    assert conf["client_secret"] and len(conf["client_id"]) > 60


def test_refresh_rotates_access_and_follows_google_revocation(server):
    c, g = server
    cid, q, v = _sign_in(c, g)
    t = _token(c, cid, q["code"][0], v).json()
    r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid})
    assert r.status_code == 200, r.text
    remote._creds.clear()
    assert _whoami(c, r.json()["access_token"])[1].startswith("gat-refreshed-")
    new_refresh = r.json()["refresh_token"]
    g.refresh_ok = False  # the user removed access in their Google account
    r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": new_refresh, "client_id": cid})
    assert r.status_code == 400


def test_a_replaced_refresh_token_is_refused(server):
    c, g = server
    cid, q, v = _sign_in(c, g)
    t = _token(c, cid, q["code"][0], v).json()
    ok = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid})
    assert ok.status_code == 200
    again = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid})
    assert again.status_code == 400  # rotation: the old one is spent


def test_revoking_an_access_token_revokes_the_google_grant(server):
    c, g = server
    cid, q, v = _sign_in(c, g)
    t = _token(c, cid, q["code"][0], v).json()
    r = c.post("/revoke", data={"token": t["access_token"], "token_type_hint": "access_token", "client_id": cid,
                                 "client_secret": ""})  # the SDK form model requires the field
    assert r.status_code == 200
    assert any(url == remote.GOOGLE_REVOKE for url, _ in g.calls)


def test_a_refused_sign_in_does_not_tell_the_client_the_email(server):
    c, g = server
    _, q, _ = _sign_in(c, g, "stranger@gmail.com")
    assert "stranger" not in q["error_description"][0]


def test_config_errors_name_what_is_missing():
    with pytest.raises(remote.ConfigError, match="GCHAT_MCP_ALLOWED_USERS"):
        remote.Config.from_env({k: v for k, v in ENV.items() if k != "GCHAT_MCP_ALLOWED_USERS"})
    with pytest.raises(remote.ConfigError, match="https"):
        remote.Config.from_env({**ENV, "GCHAT_MCP_BASE_URL": "http://gchat.example.com"})
    with pytest.raises(remote.ConfigError, match="32 characters"):
        remote.Config.from_env({**ENV, "GCHAT_MCP_SECRET": "short"})
    cfg = remote.Config.from_env(ENV)
    assert cfg.user_allowed("Someone@Example.com") and cfg.user_allowed("guest@other.org")
    assert not cfg.user_allowed("guest@example.org") and not cfg.user_allowed("")


def test_http_app_drops_send_file_and_keeps_the_rest():
    """Runs in a child process: http_app() changes the server module for good."""
    code = ("import asyncio; from gchat_mcp import server; server.http_app(); "
            "names = {t.name for t in asyncio.run(server.mcp.list_tools())}; "
            "assert 'send_file' not in names and 'send_message' in names and 'read_conversation' in names, names; "
            "assert server.REMOTE; print('ok')")
    out = subprocess.run([sys.executable, "-c", code], env={**ENV, "PATH": "/usr/bin:/bin", "HOME": "/tmp"},
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "ok", out.stderr



def test_a_code_used_on_one_replica_is_refused_on_another(replicas):
    a, b, g, fs = replicas
    cid, q, v = _sign_in(a, g)
    assert _token(a, cid, q["code"][0], v).status_code == 200
    assert _token(b, cid, q["code"][0], v).status_code == 400
    assert any(k.startswith("code-") for k in fs.docs) and not any(q["code"][0] in k for k in fs.docs)


def test_a_replaced_refresh_token_is_refused_on_every_replica(replicas):
    a, b, g, _ = replicas
    cid, q, v = _sign_in(a, g)
    t = _token(a, cid, q["code"][0], v).json()
    form = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid}
    new = a.post("/token", data=form)
    assert new.status_code == 200
    assert b.post("/token", data=form).status_code == 400  # the old one, on the other replica
    form["refresh_token"] = new.json()["refresh_token"]
    assert b.post("/token", data=form).status_code == 200  # the new one works anywhere


def test_firestore_unreachable_refuses_rather_than_allowing_reuse(replicas):
    a, _, g, fs = replicas
    cid, q, v = _sign_in(a, g)
    fs.down = True
    assert _token(a, cid, q["code"][0], v).status_code == 400


def test_a_client_sealed_with_a_dropped_key_must_register_again(server):
    c, g = server
    cid, _, _ = _sign_in(c, g)
    other = remote.Box(("z" * 40,))
    r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": "x", "client_id": other.seal(
        "client", {"r": [CLAUDE_CB], "m": "none"})})
    assert r.status_code == 401  # unknown client: the connector registers again
    forged = remote.Box((ENV["GCHAT_MCP_SECRET"],)).seal("client", {"r": ["https://evil.example/cb"], "m": "none"})
    r = c.get("/authorize", params={"response_type": "code", "client_id": forged, "redirect_uri": "https://evil.example/cb",
                                    "code_challenge": "x" * 43, "code_challenge_method": "S256"})
    assert r.status_code == 400 and "evil" not in r.headers.get("location", "")


def test_guard_from_env_picks_memory_unless_a_firestore_project_is_set(monkeypatch):
    assert isinstance(remote.guard_from_env({}), remote.MemoryGuard)
    made = {}
    monkeypatch.setattr(remote.FirestoreGuard, "__init__", lambda self, p, d: made.update(p=p, d=d))
    assert isinstance(remote.guard_from_env({"GCHAT_MCP_FIRESTORE_PROJECT": "proj"}), remote.FirestoreGuard)
    assert made == {"p": "proj", "d": "(default)"}


def test_remote_get_attachment_returns_a_pdf_as_an_embedded_resource(monkeypatch):
    from gchat_mcp import chat, server
    pdf = b"%PDF-1.7 tiny"
    monkeypatch.setattr(server, "REMOTE", True)
    monkeypatch.setattr(chat, "get", lambda *a, **k: {"attachment": [
        {"contentName": "q3.pdf", "contentType": "application/pdf", "attachmentDataRef": {"resourceName": "r1"}}]})
    monkeypatch.setattr(chat, "download", lambda resource, cap: pdf)
    res = server.get_attachment("spaces/AAA/messages/m1")
    blob = res.content[1].resource
    assert base64.b64decode(blob.blob) == pdf and blob.mimeType == "application/pdf"
    assert "saved to" not in res.content[0].text
    monkeypatch.setattr(chat, "download", lambda resource, cap: b"x" * (server.INLINE_IMAGE_CAP + 1))
    assert "open it in Google Chat" in server.get_attachment("spaces/AAA/messages/m1").content[1].text
