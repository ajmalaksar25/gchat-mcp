"""Google Chat / People REST client: retries, paginated lists, name and label resolution."""
import time

import requests
from google.auth.transport.requests import AuthorizedSession

from . import auth, fmt

CHAT = "https://chat.googleapis.com/v1"
_session = None
_me = None
_people: dict[str, dict] = {}
_labels: dict[str, str] = {}


class ApiError(RuntimeError):
    def __init__(self, call: str, status, msg: str):
        super().__init__(f"{call}: HTTP {status}: {msg}")
        self.status = status


def get(path: str, call: str, params: dict | None = None) -> dict:
    """GET a Chat path (or absolute URL), retrying 429/5xx up to 3 times; raise ApiError otherwise."""
    global _session
    if _session is None:
        _session = AuthorizedSession(auth.credentials())
    url = path if path.startswith("http") else f"{CHAT}/{path}"
    for attempt in range(4):
        try:
            r = _session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            raise ApiError(call, "network error", str(e)) from e
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        break
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        raise ApiError(call, r.status_code, msg)
    return r.json()


def paginate(path: str, key: str, call: str, params: dict, cap: int) -> tuple[list[dict], bool]:
    """Collect up to `cap` items across pages; second value says more existed."""
    out, token = [], None
    while len(out) < cap:
        d = get(path, call, {**params, **({"pageToken": token} if token else {})})
        out += d.get(key, [])
        token = d.get("nextPageToken")
        if not token:
            return out, False
    return out[:cap], bool(token) or len(out) > cap


def me_id() -> str:
    global _me
    if not _me:
        _me = get("https://www.googleapis.com/oauth2/v2/userinfo", "userinfo")["id"]
    return _me


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


def who(user) -> str:
    if isinstance(user, dict) and user.get("displayName"):
        return user["displayName"]
    return person(user)["name"]


def label(space: dict) -> str:
    """Space display name, or the other members' names for DMs / group chats."""
    if space.get("displayName"):
        return space["displayName"]
    n = space["name"]
    if n not in _labels:
        try:
            ms, _ = paginate(f"{n}/members", "memberships", "spaces.members.list", {"pageSize": 20}, 20)
            names = [who(m["member"]) for m in ms
                     if m.get("member", {}).get("type") != "BOT" and fmt.uid(m.get("member")) != me_id()]
            _labels[n] = ", ".join(names) or "(no other members)"
        except ApiError as e:
            return f"{n} (members unreadable: HTTP {e.status})"
    return _labels[n]


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
