"""gateway/room_personas.py — Agent personality definitions for the Live agent discussion room.

Each agent has a distinct voice, visual identity, and message templates
for the real-time discussion room.
"""

from __future__ import annotations

AGENT_PERSONAS: dict[str, dict[str, str]] = {
    "nigehban": {
        "display_name": "\u0646\u06AF\u06C1\u0628\u0627\u0646 (Nigehban)",
        "role": "Watchman",
        "emoji": "\U0001f52d",
        "color": "#4FC3F7",
        "voice": "Alert, concise, military-precision",
    },
    "muhaqqiq": {
        "display_name": "\u0645\u062D\u0642\u0642 (Muhaqqiq)",
        "role": "Investigator",
        "emoji": "\U0001f50d",
        "color": "#AB47BC",
        "voice": "Methodical, evidence-driven, forensic",
    },
    "mudabbir": {
        "display_name": "\u0645\u062F\u0628\u0651\u0631 (Mudabbir)",
        "role": "Strategist",
        "emoji": "\U0001f4d0",
        "color": "#66BB6A",
        "voice": "Strategic, precise, risk-aware",
    },
    "muhtasib": {
        "display_name": "\u0645\u062D\u062A\u0633\u0628 (Muhtasib)",
        "role": "Auditor",
        "emoji": "\u2696\ufe0f",
        "color": "#FFA726",
        "voice": "Skeptical, thorough, demanding evidence",
    },
    "aamil": {
        "display_name": "\u0639\u0627\u0645\u0644 (Aamil)",
        "role": "Executor",
        "emoji": "\u26a1",
        "color": "#EF5350",
        "voice": "Precise, operational, status-focused",
    },
    "system": {
        "display_name": "\U0001f510 System",
        "role": "Gateway",
        "emoji": "\U0001f510",
        "color": "#78909C",
        "voice": "Formal, authoritative",
    },
}


def get_persona(agent_name: str) -> dict[str, str]:
    """Get persona dict for an agent, falling back to system."""
    return AGENT_PERSONAS.get(agent_name, AGENT_PERSONAS["system"])


def format_room_content(
    agent_name: str, template: str, **kwargs: str,
) -> tuple[str, str, str, str]:
    """Format a room message from a template.

    Returns (content, sender_display, sender_emoji, sender_color).
    """
    persona = get_persona(agent_name)
    content = template.format(**kwargs)
    return (
        content,
        persona["display_name"],
        persona["emoji"],
        persona["color"],
    )
