import pytest

from gchat_mcp import state


@pytest.fixture(autouse=True)
def _isolated_external_state(monkeypatch):
    """Tests never reach SubQ or the developer's real gchat state unless a test opts into a file store."""
    monkeypatch.setenv("GCHAT_TRIAGE_SNAP", "off")
    monkeypatch.delenv("GCHAT_MCP_FOOTER", raising=False)
    monkeypatch.setenv("GCHAT_MCP_STATE", "memory")
    state.reset()
    yield
    state.reset()
