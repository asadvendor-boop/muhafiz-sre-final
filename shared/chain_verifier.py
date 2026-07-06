"""
shared/chain_verifier.py – Chain-Replay Audit Verifier
============================================================

Deterministic verifier that replays the event hash chain for an
incident and extracts auditable facts.  Reuses the store's canonical
hashing logic (store.verify_incident_chain) to avoid duplicating
hash computation.

Usage in tests:

    result = await verify_chain(store, incident_id)
    assert result.valid
    assert "human_approved" in result.event_types
    assert result.terminal_status == "RESOLVED"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from gateway.store import IncidentStore

logger = logging.getLogger(__name__)


@dataclass
class ChainVerification:
    """Result of a chain-replay audit verification."""

    valid: bool
    """True if every event hash links correctly to its predecessor."""

    event_count: int
    """Total number of events in the chain."""

    event_types: list[str]
    """Ordered list of event_type values in chain order."""

    terminal_event_type: str
    """The event_type of the last event in the chain."""

    terminal_status: str
    """
    The incident status implied by the terminal event.
    Extracted from the last status-changing event's payload or
    from the incident record itself.
    """

    chain_head_hash: str
    """The hash of the first event (anchored to genesis)."""

    chain_tail_hash: str
    """The hash of the last event (the seal)."""

    facts: dict = field(default_factory=dict)
    """
    Key facts extracted from event payloads:
      - actions_executed: int (from actions_executed events)
      - plan_validated: bool (plan_validated event present)
      - plan_tampered: bool (plan_tampered event present)
      - approval_claimed: bool (human_approved event present)
      - challenge_rounds: int (count of challenge_issued events)
    """


async def verify_chain(
    store: IncidentStore,
    incident_id: str,
) -> ChainVerification:
    """
    Replay the event hash chain and extract auditable facts.

    Delegates hash integrity checking to store.verify_incident_chain()
    (which uses the same canonical sha256_hex helper used at write time)
    and then walks the events to extract facts.

    Args:
        store: The IncidentStore instance.
        incident_id: The incident to verify.

    Returns:
        ChainVerification with integrity result and extracted facts.
    """
    # 1. Delegate hash integrity to the store's canonical verifier
    chain_valid = await store.verify_incident_chain(incident_id)

    # 2. Fetch all events for fact extraction
    events = await store.get_events(incident_id, after=0)

    if not events:
        return ChainVerification(
            valid=chain_valid,
            event_count=0,
            event_types=[],
            terminal_event_type="",
            terminal_status="",
            chain_head_hash="",
            chain_tail_hash="",
            facts={},
        )

    event_types = [e["event_type"] for e in events]

    # 3. Extract auditable facts from event payloads
    facts: dict = {
        "actions_executed": 0,
        "plan_validated": False,
        "plan_tampered": False,
        "approval_claimed": False,
        "challenge_rounds": 0,
    }

    for event in events:
        etype = event["event_type"]

        if etype == "actions_executed":
            payload = _parse_payload(event)
            receipts = payload.get("receipts", {})
            facts["actions_executed"] = len(receipts)

        elif etype == "plan_validated":
            facts["plan_validated"] = True

        elif etype == "plan_tampered":
            facts["plan_tampered"] = True

        elif etype == "human_approved":
            facts["approval_claimed"] = True

        elif etype == "challenge_issued":
            facts["challenge_rounds"] += 1

    # 4. Get terminal status from the incident record
    incident = await store.get_incident(incident_id)
    terminal_status = incident.get("status", "") if incident else ""

    return ChainVerification(
        valid=chain_valid,
        event_count=len(events),
        event_types=event_types,
        terminal_event_type=event_types[-1] if event_types else "",
        terminal_status=terminal_status,
        chain_head_hash=events[0].get("event_hash", ""),
        chain_tail_hash=events[-1].get("event_hash", ""),
        facts=facts,
    )


def _parse_payload(event: dict) -> dict:
    """Safely parse event payload_json."""
    raw = event.get("payload_json", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
