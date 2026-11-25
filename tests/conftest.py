import pytest


@pytest.fixture(autouse=True)
def _no_snap(monkeypatch):
    """Tests never reach SubQ unless a test turns Snap on itself."""
    monkeypatch.setenv("GCHAT_TRIAGE_SNAP", "off")
