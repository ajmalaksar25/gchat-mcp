import os

import pytest

# Tests assert times in one fixed zone; set it before gchat_mcp is imported (fmt.LOCAL is read at import).
os.environ["GCHAT_MCP_TZ"] = "Asia/Kolkata"


@pytest.fixture(autouse=True)
def _no_snap(monkeypatch):
    """Tests never reach SubQ unless a test turns Snap on itself."""
    monkeypatch.setenv("GCHAT_TRIAGE_SNAP", "off")
