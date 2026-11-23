"""A small in-memory Chat server behind the session interface, so tests exercise the real `chat._send` path:
URL building, query params, JSON/binary bodies, error-body parsing, pagination and server-side rewriting."""
import json
import re
import threading
import time

import requests

ME = "111"


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
        self.reactions = {}

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
        return self.route(method, path, params, json, body)

    def route(self, method, path, params, js, body):
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
            want = (params.get("filter") or "").split("thread.name = ")[-1]
            msgs = [x for x in self.messages.values() if not want or x["thread"]["name"] == want]
            return reply(200, {"messages": msgs[: params.get("pageSize", 100)]})
        m = re.match(r"v1/(spaces/[^/]+)/members$", path)
        if m:
            start = int(params.get("pageToken", 0))
            page = self.members[start:start + self.page_size]
            end = start + self.page_size
            return reply(200, {"memberships": page, **({"nextPageToken": str(end)} if end < len(self.members) else {})})
        if re.match(r"v1/spaces/[^/]+/messages/[^/]+$", path):
            return reply(200, self.messages[path[3:]]) if path[3:] in self.messages else reply(404, {"error": {"message": "nf"}})
        if re.match(r"v1/users/[^/]+/spaces/.+ReadState$", path):
            key = path[3:]
            if method == "PATCH":
                time.sleep(self.patch_delay(body) if callable(self.patch_delay) else self.patch_delay)
                self.markers[key] = self.rewrite(key, body["lastReadTime"])
            return reply(200, {"name": key, "lastReadTime": self.markers.get(key, "")})
        m = re.match(r"v1/(spaces/[^/]+/messages/[^/]+)/reactions$", path)
        if m and method == "POST":
            key = (m[1], body["emoji"]["unicode"])
            if key in self.reactions:
                return reply(409, {"error": {"code": 409, "message": "exists", "status": "ALREADY_EXISTS"}})
            self.reactions[key] = True
            return reply(200, {})
        if m and method == "GET":
            allr = [{"name": f"{k[0]}/reactions/{i}", "emoji": {"unicode": k[1]}, "user": {"name": f"users/{ME}"}}
                    for i, k in enumerate(self.reactions) if k[0] == m[1]]
            start, size = int(params.get("pageToken", 0)), int(params.get("pageSize", 100))
            return reply(200, {"reactions": allr[start:start + size],
                               **({"nextPageToken": str(start + size)} if start + size < len(allr) else {})})
        if re.match(r"v1/spaces/[^/]+$", path):
            return reply(200, {"name": path[3:], "displayName": "Team", "spaceType": "SPACE"})
        raise AssertionError(f"unrouted {method} {path}")
