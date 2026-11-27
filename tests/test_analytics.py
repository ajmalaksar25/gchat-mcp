"""Opt-in PostHog analytics: off without a key; when on, tool arguments and results never leave the machine."""
import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from gchat_mcp import analytics

posthog = pytest.importorskip("posthog")


def test_off_without_a_key(monkeypatch):
    monkeypatch.delenv(analytics.KEY_ENV, raising=False)
    assert analytics.enable(FastMCP("t")) is None
    monkeypatch.setenv(analytics.KEY_ENV, "off")
    assert analytics.enable(FastMCP("t")) is None


def test_unknown_event_shape_is_dropped():
    assert analytics.before_send({"event": "x"}) is None


def test_tool_call_event_has_no_arguments_or_results(monkeypatch):
    monkeypatch.setenv(analytics.KEY_ENV, "phc_test")
    monkeypatch.setattr(analytics, "_email", lambda: "sam@example.com")
    sent = []
    monkeypatch.setattr(posthog.Posthog, "capture", lambda self, event, **kw: sent.append((event, kw)))
    srv = FastMCP("t")

    @srv.tool()
    def read_conversation(conversation_id: str) -> str:
        """Read a conversation."""
        return "SECRET message text from Dana"

    client = analytics.enable(srv)
    assert client is not None

    async def go():
        async with create_connected_server_and_client_session(srv) as c:
            tools = (await c.list_tools()).tools
            await c.call_tool("read_conversation", {"conversation_id": "spaces/SECRETID",
                                                    "context": "check a reply"})
            return tools
    tools = anyio.run(go)
    client.flush = lambda: None
    calls = [kw for e, kw in sent if e == "$mcp_tool_call"]
    assert len(calls) == 1 and calls[0]["distinct_id"] == "sam@example.com"
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "read_conversation" and "$mcp_duration_ms" in props
    blob = repr(sent)
    assert "SECRET" not in blob and "$mcp_parameters" not in props and "$mcp_response" not in props
    assert "context" in tools[0].inputSchema["properties"]  # the intent line the model writes
