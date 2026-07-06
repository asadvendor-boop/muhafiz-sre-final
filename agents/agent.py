"""agents/agent.py — Agent Registry (no orchestrator).

The gateway controls routing explicitly:
  Phase 1: Nigehban → [false_alarm?] → Muhaqqiq → Mudabbir → Muhtasib
            ↖ challenge(EVIDENCE) ←──────────────────────────┘
            Muhaqqiq ← challenge(EVIDENCE)
            Mudabbir ← challenge(PLAN)
  Phase 2: Aamil (post-approval only)

Gateway-orchestrated, explicitly routed ADK multi-agent workflow — the gateway orchestrates routing to support:
  - False alarm short-circuit after Nigehban
  - Challenge routing back to Muhaqqiq/Mudabbir from Muhtasib
  - Bounded challenge rounds (max 3)

ARCHITECTURAL INVARIANT:
  Every agent in AGENT_REGISTRY MUST be a plain LlmAgent.
  No composite orchestrators (LoopAgent, SequentialAgent, GroupAgent)
  are permitted — the gateway is the sole orchestration authority.
"""

from google.adk.agents import LlmAgent

from agents.nigehban import nigehban, get_nigehban_agent
from agents.muhaqqiq import muhaqqiq, get_muhaqqiq_agent
from agents.mudabbir import mudabbir, get_mudabbir_agent
from agents.muhtasib import muhtasib, get_muhtasib_agent
from agents.aamil import aamil, get_aamil_agent

# Agent registry for gateway routing.
# Each agent is an LlmAgent invoked individually by the gateway.
AGENT_REGISTRY: dict[str, object] = {
    "nigehban": nigehban,
    "muhaqqiq": muhaqqiq,
    "mudabbir": mudabbir,
    "muhtasib": muhtasib,
    "aamil": aamil,
}

# Factory registry for agents that need per-invocation config
# (scenario_id, thinking_level, etc).
# Signature varies by agent — the gateway handles dispatching.
AGENT_FACTORIES: dict[str, object] = {
    "nigehban": get_nigehban_agent,
    "muhaqqiq": get_muhaqqiq_agent,
    "mudabbir": get_mudabbir_agent,
    "muhtasib": get_muhtasib_agent,
    "aamil": get_aamil_agent,
}

# ── Architectural invariant: gateway-only orchestration ──────────────────
# Assert at import time that no composite orchestrators have been
# accidentally introduced. The gateway MUST remain the sole authority
# for agent routing and pipeline control flow.
for _name, _agent in AGENT_REGISTRY.items():
    assert isinstance(_agent, LlmAgent), (
        f"AGENT_REGISTRY['{_name}'] is {type(_agent).__name__}, "
        f"not LlmAgent. Only LlmAgent instances are permitted — "
        f"the gateway is the sole orchestration authority."
    )

__all__ = [
    "AGENT_REGISTRY",
    "AGENT_FACTORIES",
    "nigehban",
    "get_nigehban_agent",
    "muhaqqiq",
    "get_muhaqqiq_agent",
    "mudabbir",
    "get_mudabbir_agent",
    "muhtasib",
    "get_muhtasib_agent",
    "aamil",
    "get_aamil_agent",
]
