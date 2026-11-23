"""A small in-memory Chat server behind the session interface, so tests exercise the real `chat._send` path:
URL building, query params, JSON/binary bodies, error-body parsing, pagination and server-side rewriting."""
import json
import re
import threading
import time

from datetime import datetime

import requests

ME = "111"


def parse_ts(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def reply(status: int, body=None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body if body is not None else {}).encode()
    r.headers["Content-Type"] = "application/json"
    return r


def scope_denied() -> requests.Response:
    """The body Google returns for a token that lacks the scope."""
    return reply(403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                                 "status": "PERMISSION_DENIED",
                                 "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                              "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "domain": "googleapis.com"}]}})


class ChatServer:
    def __init__(self):
        self.log = []  # (method, path, params, json-or-bytes)
        self.messages = {}  # name -> message
        self.threads = set()  # thread names that exist
        self.markers = {}  # read-state resource name -> lastReadTime
        self.members = [{"member": {"name": f"users/{i}", "type": "HUMAN"}} for i in (ME, "222", "333", "444")]
        self.page_size = 2
        self.deny = set()  # "METHOD path-regex" -> answered with the insufficient-scope 403
        self.rewrite = lambda path, value: value  # what Chat does to a marker it is given
        self.patch_delay = 0.0
        self.reactions = {}  # id -> (message name, emoji, user name)
        self._rid = 0
        self.space_type = "SPACE"
        self.threading = "THREADED_MESSAGES"
        self.spaces = [{"name": "spaces/AAA", "displayName": "Team", "spaceType": "SPACE",
                        "lastActiveTime": "2026-11-23T16:00:00Z"}]
        self.after_patch = None  # hook(message) to simulate Chat storing something else than was sent
        self.profiles = {}  # user id -> {"name":..., "email":...} served by the People API
        self.force_409 = False
        self.ignore_user_filter = False  # simulate a broken reactions.list filter
        self.break_reads = False  # after the first write, every GET answers 503
        self.wrote = False

    def add_reaction(self, msg, emoji, user=f"users/{ME}"):
        self._rid += 1
        self.reactions[self._rid] = (msg, emoji, user)

    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None, stream=False):
        path = url.split("googleapis.com/", 1)[-1]
        params = params or {}
        body = data.read() if hasattr(data, "read") else (data if data is not None else json)
        if hasattr(data, "read"):
            assert int(len(data)) == len(body), "Content-Length would not match the body"
        self.log.append((method, path, params, body, headers or {}))
        for rule in self.deny:
            m, rx = rule.split(" ", 1)
            if m == method and re.search(rx, path):
                return scope_denied()
        if method == "GET" and self.wrote and self.break_reads:
            return reply(503, {"error": {"code": 503, "message": "backend unavailable", "status": "UNAVAILABLE"}})
        out = self.route(method, path, params, json, body)
        if method != "GET" and out.status_code < 400:
            self.wrote = True
        return out

    def route(self, method, path, params, js, body):
        m = re.match(r"v1/people/(\w+)$", path.replace("https://people.googleapis.com/", "")) or re.match(r"(?:v1/)?people/(\w+)$", path)
        if m:
            prof = self.profiles.get(m[1])
            if prof is None:
                return reply(404, {"error": {"message": "no such person"}})
            return reply(200, {"names": [{"displayName": prof["name"]}] if prof.get("name") else [],
                               "emailAddresses": [{"value": prof["email"]}] if prof.get("email") else []})
        if path == "v1/people:searchDirectoryPeople":
            q = params["query"].casefold()
            return reply(200, {"people": [
                {"resourceName": f"people/{i}", "names": [{"displayName": p["name"]}],
                 "emailAddresses": [{"value": p["email"]}]} for i, p in self.profiles.items()
                if q in p["name"].casefold() or q in p["email"].casefold()]})
        if path == "v1/spaces:setup":
            return reply(200, {"name": "spaces/NEW", "spaceType": js["space"]["spaceType"], "displayName": js["space"].get("displayName", "")})
        if path.endswith("oauth2/v2/userinfo"):
            return reply(200, {"id": ME, "email": "ajmal@x.com"})
        if path.startswith("upload/v1/") and path.endswith("attachments:upload"):
            return reply(200, {"attachmentDataRef": {"attachmentUploadToken": "tok-1", "resourceName": "r"}})
        m = re.match(r"v1/(spaces/[^/]+)/messages$", path)
        if m and method == "POST":
            thread = (js.get("thread") or {}).get("name")
            if thread and params.get("messageReplyOption") == "REPLY_MESSAGE_OR_FAIL" and thread not in self.threads:
                return reply(404, {"error": {"code": 404, "message": "thread not found", "status": "NOT_FOUND"}})
            name = f"{m[1]}/messages/n{len(self.messages)}"
            self.messages[name] = {"name": name, "sender": {"name": f"users/{ME}", "type": "HUMAN"}, "text": js.get("text", ""),
                                   "createTime": "2026-11-23T14:00:00.500000Z",
                                   "thread": {"name": thread or f"{m[1]}/threads/new"},
                                   "attachment": [{"contentName": "f.bin", "contentType": "application/octet-stream"}]
                                   if js.get("attachment") else []}
            return reply(200, {"name": name})
        if m and method == "GET":
            flt = params.get("filter") or ""
            want = flt.split("thread.name = ")[-1] if "thread.name = " in flt else ""
            after = re.search(r'(?:createTime|create_time) > "([^"]+)"', flt)
            msgs = [x for x in self.messages.values() if x["name"].startswith(m[1] + "/")
                    and (not want or x["thread"]["name"] == want)
                    and (not after or parse_ts(x.get("createTime", "1970-01-01T00:00:00Z")) > parse_ts(after[1]))]
            msgs.sort(key=lambda x: parse_ts(x.get("createTime", "1970-01-01T00:00:00Z")), reverse=params.get("orderBy") == "createTime desc")
            start, size = int(params.get("pageToken", 0)), int(params.get("pageSize", 100))
            return reply(200, {"messages": msgs[start:start + size],
                               **({"nextPageToken": str(start + size)} if start + size < len(msgs) else {})})
        m = re.match(r"v1/(spaces/[^/]+)/members$", path)
        if m:
            start = int(params.get("pageToken", 0))
            page = self.members[start:start + self.page_size]
            end = start + self.page_size
            return reply(200, {"memberships": page, **({"nextPageToken": str(end)} if end < len(self.members) else {})})
        if path == "v1/spaces" and method == "GET":
            return reply(200, {"spaces": self.spaces})
        m = re.match(r"v1/(spaces/[^/]+/messages/[^/]+)$", path)
        if m and method == "PATCH":
            if m[1] not in self.messages:
                return reply(404, {"error": {"message": "nf"}})
            msg = self.messages[m[1]]
            raw = js["text"]
            # What Chat stores: `text` is plain (markup removed, mentions shown as @Name), `formattedText` keeps
            # the markup and `<users/ID>` (docs: spaces.messages, Message.formattedText).
            plain = re.sub(r"([*_~`])(\S(?:.*?\S)?)\1", r"\2", raw)
            ann = []
            while (u := re.search(r"<users/(\w+)>", plain)):
                nm = "@" + (self.profiles.get(u[1]) or {}).get("name", u[1])
                plain = plain[:u.start()] + nm + plain[u.end():]
                ann.append({"type": "USER_MENTION", "startIndex": u.start(), "length": len(nm),
                            "userMention": {"user": {"name": f"users/{u[1]}"}, "type": "MENTION"}})
            msg.update(text=plain, formattedText=raw, annotations=ann, lastUpdateTime="2026-11-23T14:05:00Z")
            if self.after_patch:
                self.after_patch(msg)
            return reply(200, msg)
        if method == "GET" and re.match(r"v1/spaces/[^/]+/messages/[^/]+$", path):
            return reply(200, self.messages[path[3:]]) if path[3:] in self.messages else reply(404, {"error": {"message": "nf"}})
        if re.match(r"v1/users/[^/]+/spaces/.+ReadState$", path):
            key = path[3:].replace("users/me/", f"users/{ME}/", 1)  # Chat accepts the `me` alias
            if method == "PATCH":
                time.sleep(self.patch_delay(body) if callable(self.patch_delay) else self.patch_delay)
                self.markers[key] = self.rewrite(key, body["lastReadTime"])
            return reply(200, {"name": key, "lastReadTime": self.markers.get(key, "")})
        m = re.match(r"v1/(spaces/[^/]+/messages/[^/]+)/reactions$", path)
        if m and method == "POST":
            if self.force_409 or any(v == (m[1], js["emoji"]["unicode"], f"users/{ME}") for v in self.reactions.values()):
                return reply(409, {"error": {"code": 409, "message": "exists", "status": "ALREADY_EXISTS"}})
            self.add_reaction(m[1], js["emoji"]["unicode"])
            return reply(200, {})
        if m and method == "GET":
            who = re.search(r'user\.name = "([^"]+)"', params.get("filter", ""))
            allr = [{"name": f"{v[0]}/reactions/{i}", "emoji": {"unicode": v[1]}, "user": {"name": v[2]}}
                    for i, v in self.reactions.items() if v[0] == m[1]
                    and (self.ignore_user_filter or not who or v[2] == who[1])]
            start, size = int(params.get("pageToken", 0)), int(params.get("pageSize", 100))
            return reply(200, {"reactions": allr[start:start + size],
                               **({"nextPageToken": str(start + size)} if start + size < len(allr) else {})})
        m = re.match(r"v1/spaces/[^/]+/messages/[^/]+/reactions/(\d+)$", path)
        if m and method == "DELETE":
            self.reactions.pop(int(m[1]), None)
            return reply(200, {})
        m = re.match(r"v1/(spaces/[^/]+/messages/[^/]+)$", path)
        if m and method == "DELETE":
            self.messages[m[1]]["deleteTime"] = "2026-11-23T14:06:00Z"
            return reply(200, {})
        if re.match(r"v1/spaces/[^/]+$", path):
            return reply(200, {"name": path[3:], "displayName": "Team", "spaceType": self.space_type,
                               "spaceThreadingState": self.threading})
        raise AssertionError(f"unrouted {method} {path}")
