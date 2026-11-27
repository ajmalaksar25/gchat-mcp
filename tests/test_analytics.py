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

    @srv.tool()
    def fail() -> str:
        """Fail like an ownership check."""
        raise ValueError("SECRET: that message was sent by Dana Lee, not you")

    client = analytics.enable(srv)
    assert client is not None

    async def go():
        async with create_connected_server_and_client_session(srv) as c:
            tools = (await c.list_tools()).tools
            await c.call_tool("read_conversation", {"conversation_id": "spaces/SECRETID"})  # no context: still runs
            await c.call_tool("fail", {})
            return tools
    tools = anyio.run(go)
    client.flush = lambda: None
    calls = [kw for e, kw in sent if e == "$mcp_tool_call"]
    assert len(calls) == 2 and calls[0]["distinct_id"] == "sam@example.com"
    assert calls[1]["properties"]["$mcp_is_error"] and "$mcp_error_message" not in calls[1]["properties"]
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "read_conversation" and "$mcp_duration_ms" in props
    blob = repr(sent)
    assert "SECRET" not in blob and "$mcp_parameters" not in props and "$mcp_response" not in props
    assert all("context" not in t.inputSchema.get("required", []) for t in tools)  # schemas stay as they were
    assert {kw["properties"].get("deployment") for e, kw in sent} == {"local"}  # on every event, not only calls


def test_deployment_is_validated_and_set(monkeypatch):
    monkeypatch.setenv(analytics.KEY_ENV, "phc_test")
    with pytest.raises(ValueError):
        analytics.enable(FastMCP("t"), deployment="cloud")
    analytics.enable(FastMCP("t"), deployment="hosted")
    assert analytics.before_send({"event": "x", "properties": {}})["properties"]["deployment"] == "hosted"
    analytics.enable(FastMCP("t"), deployment="local")
