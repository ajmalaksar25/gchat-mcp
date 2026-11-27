"""Google Chat / People REST client: retries, paginated lists, name and label resolution."""
import contextvars
import hashlib
import io
import json
import threading
import time
import uuid
from contextlib import contextmanager

import requests
from google.auth.transport.requests import AuthorizedSession

from . import auth, fmt

CHAT = "https://chat.googleapis.com/v1"
_sessions: dict[str, AuthorizedSession] = {}
_identities: dict[str, dict] = {}
_lock = threading.Lock()
_people: dict[str, dict] = {}
_labels: dict[str, str] = {}


_call_box: contextvars.ContextVar[dict | None] = contextvars.ContextVar("gchat_call_box", default=None)


def _acquire() -> tuple[AuthorizedSession, str]:
    """(session, grant key) from `auth.credentials()`. Sessions are cached per grant (hash of client id +
    refresh token), never process-wide, so another caller's credentials get their own session and `me`."""
    with _lock:
        creds = auth.credentials()
        key = hashlib.sha256(f"{creds.client_id}\0{creds.refresh_token or creds.token}".encode()).hexdigest()
        if key not in _sessions:
            _sessions[key] = AuthorizedSession(creds)
        return _sessions[key], key


@contextmanager
def call():
    """One tool call: credentials are loaded once (on first use) and the same session and grant serve every
    request and the `me` lookup of the call, including pool threads that run in a copy of this context."""
    if _call_box.get() is not None:  # nested tool call (add_reaction -> react): keep the outer grant
        yield
        return
    token = _call_box.set({})
    try:
        yield
    finally:
        _call_box.reset(token)


def _session_for() -> tuple[AuthorizedSession, str]:
    box = _call_box.get()
    if box is None:  # outside a tool call: still one lookup per request, never cached across callers
        return _acquire()
    with _lock:
        have = box.get("grant")
    if have is None:
        got = _acquire()
        with _lock:
            have = box.setdefault("grant", got)
    return have


class ApiError(RuntimeError):
    def __init__(self, call: str, status, msg: str, reason: str = ""):
        super().__init__(f"{call}: HTTP {status}: {msg}")
        self.status, self.reason, self.msg = status, reason, msg

    @property
    def scope_missing(self) -> bool:
        """True for a 403 caused by the saved token lacking a scope (the only reliable signal: token.json
        records requested scopes, not granted ones). Other 403s are real permission refusals."""
        return self.status == 403 and (self.reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
                                       or "insufficient authentication scopes" in self.msg.lower())


def _send(path: str, call: str, params: dict | None = None, stream: bool = False,
          json: dict | None = None, method: str = "", data=None, headers: dict | None = None,
          timeout: float | tuple = (5, 15), retry: bool = True) -> requests.Response:
    """Call a Chat (or other Google) path, retrying 429/5xx up to 3 times; raise ApiError otherwise.

    Method defaults to POST when a body is given, else GET. POST retries are safe only with an idempotency
    key such as messages.create `requestId`; pass retry=False for non-idempotent bodies (uploads).
    """
    session, _ = _session_for()
    url = path if path.startswith("http") else f"{CHAT}/{path}"
    method = method or ("POST" if json is not None or data is not None else "GET")
    for attempt in range(4):
        try:
            r = session.request(method, url, params=params, json=json, data=data, headers=headers,
                                 timeout=timeout, stream=stream)
        except requests.RequestException as e:
            if retry and method == "GET" and attempt < 2:  # a network blip: retry a read at once, not after a backoff
                time.sleep(1)
                continue
            raise ApiError(call, "network error", str(e)) from e
        if retry and r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        break
    if r.status_code >= 400:
        reason = ""
        try:
            err = r.json()["error"]
            msg = err["message"]
            reason = next((d["reason"] for d in err.get("details", []) if isinstance(d, dict) and d.get("reason")), "")
        except Exception:
            msg = r.text[:200]
        raise ApiError(call, r.status_code, msg, reason)
    return r


def get(path: str, call: str, params: dict | None = None) -> dict:
    return _send(path, call, params).json()


def post(path: str, call: str, params: dict, body: dict) -> dict:
    return _send(path, call, params, json=body).json()


def patch(path: str, call: str, params: dict, body: dict) -> dict:
    return _send(path, call, params, json=body, method="PATCH").json()


def delete(path: str, call: str, params: dict | None = None) -> None:
    _send(path, call, params, method="DELETE")


class Multipart:
    """File-like `multipart/related` body (JSON metadata part, then the file) streamed with a known length."""

    def __init__(self, metadata: dict, mimetype: str, file, size: int, boundary: str = ""):
        self.boundary = boundary or uuid.uuid4().hex
        head = (f"--{self.boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n--{self.boundary}\r\nContent-Type: {mimetype}\r\n\r\n").encode()
        self._parts = [io.BytesIO(head), file, io.BytesIO(f"\r\n--{self.boundary}--\r\n".encode())]
        self._len = len(head) + size + len(self._parts[2].getvalue())

    def __len__(self) -> int:
        return self._len

    def read(self, n: int = -1) -> bytes:
        out = b""
        while self._parts and (n < 0 or len(out) < n):
            chunk = self._parts[0].read(-1 if n < 0 else n - len(out))
            if chunk:
                out += chunk
            else:
                self._parts.pop(0)
        return out

    @property
    def content_type(self) -> str:
        return f"multipart/related; boundary={self.boundary}"


def upload(space: str, filename: str, mimetype: str, file, size: int) -> dict:
    """media.upload (multipart, one request, no retry): returns the `attachmentDataRef` for messages.create."""
    body = Multipart({"filename": filename}, mimetype, file, size)
    r = _send(f"https://chat.googleapis.com/upload/v1/{space}/attachments:upload", "media.upload",
              {"uploadType": "multipart"}, data=body, headers={"Content-Type": body.content_type},
              timeout=(10, max(60, size >> 20)), retry=False)
    return r.json()["attachmentDataRef"]


def reactions_page(msg_name: str, user: str = "", cap: int = 25) -> tuple[list[dict], bool]:
    """Reactions on one message (each has its resource `name`), optionally only those by `user` (`users/{id}`);
    second value says more existed beyond `cap`."""
    params = {"pageSize": min(cap, 200), **({"filter": f'user.name = "{user}"'} if user else {})}
    return paginate(f"{msg_name}/reactions", "reactions", "reactions.list", params, cap)


def reactions(msg_name: str, user: str = "", cap: int = 25) -> list[dict]:
    return reactions_page(msg_name, user, cap)[0]


def download(resource: str, cap: int) -> bytes:
    """Bytes of an uploaded attachment via media.download; ApiError when larger than `cap`."""
    r = _send(f"media/{resource}", "media.download", {"alt": "media"}, stream=True)
    data = bytearray()
    for chunk in r.iter_content(1 << 16):
        data += chunk
        if len(data) > cap:
            r.close()
            raise ApiError("media.download", "too large", f"attachment exceeds {cap // (1 << 20)} MB")
    return bytes(data)


def paginate(path: str, key: str, call: str, params: dict, cap: int) -> tuple[list[dict], bool]:
    """Collect up to `cap` items across pages; second value says more existed."""
    out, token = [], None
    while len(out) < cap:
        d = get(path, call, {**params, **({"pageToken": token} if token else {})})
        out += d.get(key, [])
        token = d.get("nextPageToken")
        if not token:
            return out[:cap], len(out) > cap
    return out[:cap], bool(token) or len(out) > cap


def _userinfo() -> dict:
    _, key = _session_for()
    if key not in _identities:
        _identities[key] = get("https://www.googleapis.com/oauth2/v2/userinfo", "userinfo")
    return _identities[key]


def me_id() -> str:
    return _userinfo()["id"]


def me_email() -> str:
    return _userinfo().get("email", "")


def person(user) -> dict:
    """{'name','email'} for a user via People API (cached); falls back to 'Unknown', never raises."""
    i = fmt.uid(user)
    if i not in _people:
        info = {"name": "Unknown", "email": ""}
        if i:
            try:
                p = get(f"https://people.googleapis.com/v1/people/{i}", "people.get",
                        {"personFields": "names,emailAddresses"})
                email = next((e["value"] for e in p.get("emailAddresses", [])), "")
                info = {"name": next((n["displayName"] for n in p.get("names", [])), email or "Unknown"),
                        "email": email}
            except ApiError:
                pass
        _people[i] = info
    return _people[i]


def person_strict(user) -> dict:
    """Like `person`, but ApiError when the profile cannot be read (never a silent 'Unknown')."""
    i = fmt.uid(user)
    if i not in _people or _people[i]["name"] == "Unknown":
        p = get(f"https://people.googleapis.com/v1/people/{i}", "people.get", {"personFields": "names,emailAddresses"})
        email = next((e["value"] for e in p.get("emailAddresses", [])), "")
        _people[i] = {"name": next((n["displayName"] for n in p.get("names", [])), email or "Unknown"), "email": email}
    return _people[i]


def profile_fresh(user) -> dict:
    """{'name','email'} read from the People API now: no cache, no fallback (name '' when there is none);
    ApiError when the profile cannot be read."""
    p = get(f"https://people.googleapis.com/v1/people/{fmt.uid(user)}", "people.get",
            {"personFields": "names,emailAddresses"})
    return {"name": next((n.get("displayName", "") for n in p.get("names", [])), "").strip(),
            "email": next((e["value"] for e in p.get("emailAddresses", [])), "")}


def who(user) -> str:
    if isinstance(user, dict) and user.get("displayName"):
        return user["displayName"]
    return person(user)["name"]


def label(space: dict) -> str:
    """Space display name, or the other members' names for DMs / group chats."""
    if space.get("displayName"):
        return space["displayName"]
    n = space["name"]
    key = f"{me_id()}\0{n}"  # the label leaves out "me", so it differs per signed-in user (remote mode)
    if key not in _labels:
        try:
            ms, _ = paginate(f"{n}/members", "memberships", "spaces.members.list", {"pageSize": 20}, 20)
            names = [who(m["member"]) for m in ms
                     if m.get("member", {}).get("type") != "BOT" and fmt.uid(m.get("member")) != me_id()]
            _labels[key] = ", ".join(names) or "(no other members)"
        except ApiError as e:
            return f"{n} (members unreadable: HTTP {e.status})"
    return _labels[key]


def messages(space: str, since: str = "", thread: str = "", cap: int = 500) -> tuple[list[dict], bool]:
    """Newest-first messages in a space, optionally after `since` (RFC 3339) and/or within a thread."""
    flt = [f'create_time > "{since}"'] if since else []
    if thread:
        flt.append(f"thread.name = {thread}")
    params = {"pageSize": min(cap, 1000), "orderBy": "createTime desc"}
    if flt:
        params["filter"] = " AND ".join(flt)
    return paginate(f"{space}/messages", "messages", "spaces.messages.list", params, cap)


def spaces() -> tuple[list[dict], bool]:
    s, trunc = paginate("spaces", "spaces", "spaces.list", {"pageSize": 1000}, 3000)
    return sorted(s, key=lambda x: x.get("lastActiveTime", ""), reverse=True), trunc


def member_ids(space: str, cap: int = 1000) -> set[str]:
    """User ids (human members) of a space, read through every page; ApiError if there are more than `cap`."""
    ms, more = paginate(f"{space}/members", "memberships", "spaces.members.list", {"pageSize": 100}, cap)
    if more:
        raise ApiError("spaces.members.list", "too many", f"more than {cap} members: cannot list them all")
    return {fmt.uid(m.get("member")) for m in ms if m.get("member", {}).get("type") != "BOT"} - {""}


def directory_search(query: str, cap: int = 20) -> list[dict]:
    """Directory people matching `query`: [{'id','name','emails'}]. [] when the directory cannot be searched."""
    try:
        d = get("https://people.googleapis.com/v1/people:searchDirectoryPeople", "people.searchDirectoryPeople",
                {"query": query, "readMask": "names,emailAddresses", "pageSize": cap,
                 "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"})
    except ApiError:
        return []
    return [{"id": p["resourceName"].split("/")[-1],
             "name": next((n["displayName"] for n in p.get("names", [])), ""),
             "emails": [e["value"] for e in p.get("emailAddresses", [])]} for p in d.get("people", [])]
