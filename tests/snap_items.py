"""Made-up chat items (no real chat text) for the recorded SubQ Snap call."""
from gchat_mcp import triage


def made_up() -> list[triage.Item]:
    base = {"conversation_type": "DIRECT_MESSAGE", "thread_id": "T", "sent_at": "2026-11-25T06:00:00+00:00",
            "sent_at_local": "Wed 25 Nov 11:30", "trigger": triage.TRIGGERS["a"], "new": True}
    return [
        triage.Item(id="spaces/FAKE1/messages/m1", conversation_id="spaces/FAKE1", conversation="Dana Fox",
                    conversation_url="https://chat.google.com/dm/FAKE1", sender="Dana Fox",
                    message="Can you send me the signed vendor contract by 4 pm today? Legal is waiting on it.",
                    context=[triage.ContextMessage(sender="Me", is_me=True, sent_at="2026-11-25T05:00:00+00:00",
                                                   text="I'll review the vendor contract this morning.")], **base),
        triage.Item(id="spaces/FAKE2/messages/m2", conversation_id="spaces/FAKE2", conversation="Lee Park",
                    conversation_url="https://chat.google.com/dm/FAKE2", sender="Lee Park", message="thanks!",
                    context=[triage.ContextMessage(sender="Me", is_me=True, sent_at="2026-11-25T05:30:00+00:00",
                                                   text="Here is the deck you asked for.")], **base),
    ]
