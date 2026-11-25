"""SubQ Snap and the push watcher. Snap is checked against one recorded real call (tests/fixtures/
snap_recording.json, made-up messages); the order and watcher tests replay that recorded response from a local
HTTP listener, so the real `requests` path runs."""
import copy
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import anyio
import pytest

from gchat_mcp import snap, watch
from tests.snap_items import made_up
from tests.test_triage import SINCE, call, dm, ids, put, sampler, srv  # noqa: F401 (srv is a fixture)

REC = json.loads((Path(__file__).parent / "fixtures" / "snap_recording.json").read_text())


def test_request_matches_the_recorded_call():
    assert REC["endpoint"] == snap.URL and REC["request"]["model"] == snap.MODEL
    assert snap.body(made_up(), "Sam Rivera") == REC["request"]


def test_recorded_answer_classifies_confident_items_without_drafts():
    items = made_up()
    found, low = snap.verdicts(items, REC["response"])
    assert low == 0
    assert {k: v[0] for k, v in found.items()} == {"spaces/FAKE1/messages/m1": "reply_needed",
                                                   "spaces/FAKE2/messages/m2": "no_reply_needed"}
    assert all(v[2] is None for v in found.values())  # Snap writes no drafts


def test_below_threshold_is_left_unclassified_never_guessed():
    ans = copy.deepcopy(REC["response"])
    ans["answers"]["i1"]["confidence"] = snap.MIN_CONFIDENCE - 0.01
    ans["answers"]["i0"]["confidence"] = snap.MIN_CONFIDENCE
    found, low = snap.verdicts(made_up(), ans)
    assert list(found) == ["spaces/FAKE1/messages/m1"] and low == 1


@pytest.fixture
def snap_on(monkeypatch):
    monkeypatch.delenv("GCHAT_TRIAGE_SNAP")
    monkeypatch.setenv("SUBQ_API_KEY", "test-key")


def test_snap_failure_or_hang_leaves_items_unclassified(snap_on, monkeypatch):
    monkeypatch.setattr(snap, "TIMEOUT", (1, 1))
    with socket.socket() as silent:  # accepts the connection, never answers
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        for url in (f"http://127.0.0.1:{silent.getsockname()[1]}/v1/systemone", "http://127.0.0.1:9/v1/systemone"):
            monkeypatch.setattr(snap, "URL", url)
            items = made_up()
            notes = snap.classify(items, "Sam")
            assert all(i.category is None and i.classified_by == "client" for i in items)
            assert len(notes) == 1 and "SubQ Snap failed for 2 item(s)" in notes[0] and "test-key" not in notes[0]


def test_snap_off_switch_and_missing_key(monkeypatch, tmp_path):
    monkeypatch.setattr(snap, "KEY_FILE", tmp_path / "none")
    monkeypatch.setenv("SUBQ_API_KEY", "k")
    assert snap.key() is None  # GCHAT_TRIAGE_SNAP=off (conftest)
    monkeypatch.delenv("GCHAT_TRIAGE_SNAP")
    assert snap.key() == "k"
    monkeypatch.delenv("SUBQ_API_KEY")
    assert snap.key() is None


@pytest.fixture
def replay(snap_on, monkeypatch):
    """A local listener that answers every Snap call with the recorded response, with the low answer patched in."""
    hits, answer = [], copy.deepcopy(REC["response"])

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append((self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            out = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    monkeypatch.setattr(snap, "URL", f"http://127.0.0.1:{s.server_port}/v1/systemone")
    yield hits, answer
    s.shutdown()


# srv's items reach Snap newest first: i0 = DM2/b2, i1 = DM1/a2.
def test_order_sampling_first_snap_never_called(srv, replay):
    hits, _ = replay
    res = call("inbox_triage", {"since": SINCE}, sampler())
    assert res["classified_by"] == "sampling" and hits == []
    assert {i["classified_by"] for i in res["items"]} == {"sampling"}


def test_order_snap_when_sampling_is_refused(srv, replay):
    from mcp import types
    hits, _ = replay
    res = call("inbox_triage", {"since": SINCE},
               sampler(answer=lambda rows: types.ErrorData(code=-1, message="User rejected sampling request")))
    assert res["classified_by"] == "snap" and len(hits) == 1 and hits[0][0] == "Bearer test-key"
    by = {i["id"]: (i["category"], i["classified_by"], i["draft_reply"]) for i in res["items"]}
    assert by == {"spaces/DM2/messages/b2": ("reply_needed", "snap", None),
                  "spaces/DM1/messages/a2": ("no_reply_needed", "snap", None)}
    assert res["instructions"] is None


def test_order_client_gets_what_snap_was_unsure_about(srv, replay):
    _, answer = replay
    answer["answers"]["i1"]["confidence"] = 0.5
    res = call("inbox_triage", {"since": SINCE})
    assert res["classified_by"] == "client" and "Classify each item yourself" in res["instructions"]
    by = {i["id"]: (i["category"], i["classified_by"]) for i in res["items"]}
    assert by == {"spaces/DM2/messages/b2": ("reply_needed", "snap"), "spaces/DM1/messages/a2": (None, "client")}
    assert any("below confidence" in n for n in res["notes"])


# --- watcher
def run_watch(only=("reply_needed", "unclassified")):
    out = io.StringIO()
    code = anyio.run(lambda: watch.run(set(only), 0, SINCE, 0, polls=1, out=out))
    assert code == 0
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_watch_prints_one_line_per_new_item_and_never_twice_across_restarts(srv):
    first = run_watch()
    assert [x["message_id"] for x in first] == ["spaces/DM2/messages/b2", "spaces/DM1/messages/a2"]
    assert set(first[0]) == {"category", "classified_by", "sender", "conversation", "conversation_id", "message_id",
                             "link", "text", "sent_at_local"}
    assert first[0]["category"] == "unclassified" and first[0]["text"] == "ok"
    assert run_watch() == []  # a restarted watcher shares the state: nothing again
    put(srv, "DM2", "b3", "2026-11-23T13:00:00Z", "333", "can you resend it?")
    srv.spaces[1]["lastActiveTime"] = "2026-11-23T13:00:00Z"
    assert [x["message_id"] for x in run_watch()] == ["spaces/DM2/messages/b3"]
    assert call("inbox_triage", {"since": SINCE, "only_new": True})["items"] == []  # the MCP tool agrees


def test_watch_filter_records_classified_items_quietly(srv, replay):
    lines = run_watch(only=("reply_needed",))
    assert [(x["message_id"], x["category"], x["classified_by"]) for x in lines] == [
        ("spaces/DM2/messages/b2", "reply_needed", "snap")]
    assert run_watch(only=("reply_needed", "no_reply_needed")) == []  # the no_reply_needed one was recorded


def test_watch_scans_every_active_space_past_the_tool_cap(srv):
    from gchat_mcp import server
    extra = server.MAX_SPACES + 1
    for n in range(extra):
        dm(srv, f"X{n}", last_active="2026-11-23T08:00:00Z")
        put(srv, f"X{n}", "c1", "2026-11-23T08:00:00Z", "444", f"question {n}?")
    assert len(run_watch()) == extra + 2


def test_watch_backs_off_on_errors_and_stops_on_auth(srv, monkeypatch):
    from gchat_mcp import auth, server
    calls = []

    async def boom(*a):
        calls.append(1)
        raise (RuntimeError("503 from Chat") if len(calls) == 1 else auth.AuthError("run gchat-mcp login"))
    monkeypatch.setattr(server, "poll", boom)
    sleeps = []

    async def nap(t):
        sleeps.append(t)
    monkeypatch.setattr(watch.anyio, "sleep", nap)
    assert anyio.run(lambda: watch.run({"reply_needed"}, 30, polls=5)) == 2
    assert sleeps == [60] and len(calls) == 2

