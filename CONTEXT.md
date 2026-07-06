# MuhafizSRE — Capstone Project Context

> Submission for the **Kaggle AI Agents: Intensive Vibe Coding Capstone Project**
> Track: **Agents for Business**

---

## Competition Context

The Kaggle AI Agents capstone challenges participants to build a real-world AI agent system that demonstrates mastery of multi-agent orchestration, tool use, safety guardrails, and evaluation methodology. MuhafizSRE addresses the **Agents for Business** track by tackling human-governed Site Reliability Engineering (SRE) — a domain where AI agents can deliver measurable business value by reducing incident response times.

## What Makes This Submission Unique

### 1. Cryptographic Audit Trail
Unlike most agent systems that rely on log files, MuhafizSRE implements a **SHA-256 hash-chained event ledger** where every agent action is cryptographically linked to the previous one. This provides tamper-evident auditability with human-governed security and enterprise change-control safeguards.

### 2. Approval Contract Model
The human-in-the-loop isn't just a "confirm/deny" button — it's a **cryptographic contract system** with:
- HMAC-SHA256 signed tokens with structured claims
- Single-use nonces preventing replay attacks
- TTL expiry preventing stale approvals
- Token digests (never raw tokens) persisted for audit

### 3. Defense-in-Depth Against Prompt Injection
The system demonstrates a layered defense against adversarial inputs:
- **Telemetry Sanitizer** — 13 regex patterns flag adversarial payloads in MCP data before agent review
- **Action Policy Engine** — Skill allowlists, service allowlists, bounded parameters
- **Safety Review Agent (Muhtasib)** — Independent AI review on Gemini 3.1 Pro Preview with typed challenge targets
- **HMAC Approval Contracts** — Execution bounded by typed skills, safety review, and cryptographic contracts

### 4. Honest Disclosure
We explicitly document what is production-quality engineering vs. simulated (see `docs/honest-disclosure.md`). This transparency is itself a differentiator — we believe judges value honesty over theater.

### 5. Named Agent Fleet with Cultural Identity
Each agent has a meaningful Urdu name reflecting its role — Nigehban (Watchman), Muhaqqiq (Investigator), Mudabbir (Strategist), Muhtasib (Auditor), Aamil (Executor) — creating a cohesive narrative that makes the system memorable.

## Technical Choices Rationale

| Choice | Rationale |
|--------|-----------|
| **Google ADK 2.x** | First-party framework for Gemini agents; gateway orchestration drives explicitly routed multi-agent workflows |
| **FastAPI + SSE** | Async-first Python framework; SSE provides real-time updates without WebSocket complexity |
| **SQLite (aiosqlite)** | Zero-config embedded database; perfect for capstone demos. Production would use PostgreSQL |
| **MCP (Model Context Protocol)** | Standard protocol for tool integration; demonstrates real MCP server implementation |
| **HMAC-SHA256** | Industry-standard for token signing; deterministic (no key pair management needed) |
| **Next.js** | React framework with server components; rapid dashboard development |

## Google ADK / Antigravity Usage

MuhafizSRE uses Google ADK 2.x for agent orchestration:

- **Multi-Agent Workflow** — Gateway-orchestrated, explicitly routed ADK workflow running all 5 sub-agents in order
- **LlmAgent** — Each sub-agent (Nigehban, Muhaqqiq, Mudabbir, Muhtasib, Aamil)
- **FunctionTool** — Atomic commit tools for each agent to write to the event ledger
- **MCPToolset** — Muhaqqiq connects to the MCP telemetry server for investigation
- **Structured Instructions** — Each agent has detailed system prompts with output format specifications

## Key Metrics

| Metric | Value |
|--------|-------|
| **Test Coverage** | 245 backend tests + 35 dashboard tests, all passing |
| **API Endpoints** | 12 routes |
| **Agent Fleet** | 5 specialized agents |
| **Remediation Skills** | 6 async adapters |
| **Evaluation Scenarios** | 7 deterministic scenarios × 3 repetitions = 21 benchmark runs |
| **Injection Patterns** | 13 detection patterns |
| **Documentation** | 7 technical docs + README |
| **Lines of Python** | ~21,000 across core modules and tests |

---

*Built with Google ADK, Gemini, FastAPI, and Next.js for the Kaggle AI Agents Capstone 2026.*

---

## Production Hardening Roadmap

MuhafizSRE is a governed multi-agent SRE prototype with deterministic synthetic enterprise telemetry, real ADK agent workflows, HMAC-bound human approval contracts, and real local Docker sandbox remediation. For Fortune-500 production deployment, the next hardening steps are:

1. **Durable orchestration** — Move long-running agent workflows from FastAPI in-process background tasks to Temporal, Cloud Tasks, Celery, or Step Functions.
2. **Backpressure and rate limits** — Add tenant-level concurrency controls, LLM quota management, alert deduplication, priority queues, and budget ceilings.
3. **Production data plane** — Replace SQLite with Postgres/Cloud SQL, add row-level tenant isolation, and anchor terminal event seals to external append-only storage.
4. **Multi-tenant identity** — Add organization_id/workspace_id to incidents, contracts, events, and room messages. Enforce RBAC from authenticated JWT claims.
5. **Real infrastructure adapters** — Replace simulated enterprise skill adapters with parameterized Kubernetes, GCP, Secret Manager, PagerDuty, and ServiceNow integrations.
6. **Resumable execution** — Add idempotent action checkpoints, retry policies, compensation actions, and operator-controlled resume/reissue flows.

