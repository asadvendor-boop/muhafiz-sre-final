# MuhafizSRE: AI Investigates. Operators Authorize. The Contract Decides What Executes.

**Five Agents. One Approval Gate. Zero Unauthorized Mutations.**

*Kaggle AI Agents: Intensive Vibe Coding Capstone Project — Track 2: Agents for Business*

**Demo video:** https://www.youtube.com/watch?v=lXDDH1bb5ZI

---

## Overview

**Problem:** Production incidents still burn manual MTTR because teams can detect faster than they can execute safely.

**Solution:** A governed AI incident council on Google ADK with one operator authorization gate and contract-bound execution.

### Evidence Snapshot

- **21 / 21** controlled live scenarios passed.
- **Live safety boundary checks:** tampered token **403**, tampered revision **409**, duplicate approval **409**, rejected contract blocked.
- **Real execution proof:** victim container moves **503 → 200** only after signed operator authorization.
- **Governance tradeoff measured:** removing Muhtasib removes **34** safety-review rounds and **16** challenges, but sends **36** actions to the operator without independent safety review.
- **Test depth:** **245** Python tests + **35** dashboard frontend tests passing, covering hash-chain integrity, policy enforcement, and authorization defects.

## The Problem

Production incidents are expensive, noisy, and risky to automate. When a P0 alert fires at 3 AM, an SRE must manually read logs, check metrics, cross-reference deployments, guess root cause, plan a fix, get approval, execute, verify, and document everything. Typical MTTR stretches well beyond acceptable limits while revenue burns. Gartner's widely cited estimate puts unplanned downtime at $5,600 per minute — every minute of manual triage, escalation, and approval-hunting has a direct cost.

The compliance problem is equally severe. Enterprise auditors require **verifiable proof** that every production-changing remediation was authorized — not just a Slack "LGTM." Most teams fail audits because their approval trails are informal and unverifiable.

**The gap:** Existing alerting, observability, and ITSM tools often stop at detection, dashboards, tickets, or runbooks. MuhafizSRE demonstrates an end-to-end AI-assisted pipeline that connects triage → diagnosis → planning → safety review → operator authorization → execution → verification into a single tamper-evident workflow.

---

## The Solution

MuhafizSRE (Urdu: محافظ — "The Guardian") is an **AI Incident Council** for enterprise SRE teams. Five specialized agents triage, investigate, plan, and challenge remediation autonomously, but **only an operator-authorized contract can mutate infrastructure**. Every executed action is cryptographically bound to the approved contract.

> **In the demo:** A Docker victim service fails with HTTP 503, the agents diagnose and plan a rollback, the operator authorizes the exact signed contract, and the service recovers to HTTP 200 — with a verifiable audit chain proving every step.

### What the AI does autonomously

The agents handle the cognitive heavy-lifting without asking the operator:

1. **Nigehban** detects — triages the alert, filters noise from real incidents
2. **Muhaqqiq** investigates — queries MCP telemetry tools for logs, metrics, and deployments
3. **Mudabbir** plans — generates a remediation strategy with a SHA-256 plan nonce
4. **Muhtasib** challenges — adversarially reviews the plan against 15 safety policy rules
5. The system generates a **bound contract** linking incident, plan, actions, hash, and TTL

### What the operator authorizes

The operator reviews a **distilled decision package** — root cause, evidence, risk assessment, safe revised plan, exact contract, expected blast radius — and makes one decision:

- **Approve & Execute** — Proceed with the exact plan
- **Reject & Revise** — Send back for a new strategy
- **False Alarm** — Close without action

Then **Aamil** executes *only* what was authorized. If the plan changes, the contract no longer matches.

> **Honest Framing**: MuhafizSRE uses deterministic synthetic enterprise telemetry for reproducible evaluation and real local Docker sandbox remediation for execution proof. It is a capstone prototype, not a production Kubernetes operator.

---

## Proof: Measured Results

| Proof | Result |
|-------|--------|
| 21-run benchmark | **21/21 PASS** |
| Live sandbox recovery | **200 → 503 → approved rollback → 200** |
| Rejection check | **REJECTED**, victim stayed 503 |
| Tampered token | **403**, no mutation |
| Tampered revision | **409**, no mutation |
| Duplicate approval | **409**, no replay |

### Controlled Baseline Contrast: Safety vs Throughput

The most telling comparison is the live ablation that isolates safety governance:

| Metric | Full Pipeline | No-Muhtasib Baseline |
|---|---:|---:|
| Runs passed | 21/21 | 21/21 |
| Critical failures | 0/105 | 0/105 |
| Safety review rounds | 34 | 0 |
| Challenges issued | 16 | 0 |
| Unreviewed first-draft plans | 0 | 18 |
| Unreviewed operational actions | 0 | 36 |
| Tokens | 1,439,736 | 1,013,018 |
| Runtime | 1674.7s | 567.9s |

Interpretation: safety review does not add a new correctness class on this fixed dataset; it adds governance quality by shifting dangerous decisions back into structured, machine-enforced challenge before execution.

### Proof A: Controlled Live Benchmark (21/21)
Across 21 controlled live evaluations using deterministic telemetry fixtures with real LLM calls, MuhafizSRE achieved:
- **21/21 scenario success** (7 scenarios × 3 repetitions)
- All 7 scenarios: `bad_deployment`, `cache_stampede`, `false_positive`, `expired_credential`, `rejection_path`, `prompt_injection`, `multi_action_failure`
- Analytical agents: `gemini-3-flash-preview` | Safety agent: `gemini-3.1-pro-preview`
- Challenge loop fired in scenarios where plan violated safety policies; revised plans passed

### Proof B: Live Sandbox Smoke Test
- Victim container: healthy (200) → fault injected → unhealthy (503)
- Pipeline: Nigehban → Muhaqqiq → Mudabbir → Muhtasib (CHALLENGE → revision → APPROVED) → Operator Authorization → Aamil
- Aamil executed real HTTP POST to `http://victim:9000/recover` → `is_real_mutation: true`
- Recovery verification: `score 1.00 (8/8)` — victim confirmed healthy (200)
- Incident sealed: RESOLVED with tamper-evident hash chain

### Proof C: Negative Authority Check
- Same pipeline, but the operator **REJECTED** the contract
- Aamil never executed — victim remained unhealthy (503)
- Proves the operator authority boundary is real: **no mutation without approval**

### Muhtasib Ablation: Why the Safety Reviewer Matters

To test whether Muhtasib adds real value, a controlled comparison was run against
a baseline that bypasses Muhtasib and routes plans directly to the operator gate. The
full table is shown in **Controlled Baseline Contrast** above, and the key result is
unchanged: safety is moved from the operator to a specialized review agent before
execution, at measured model and runtime cost.

---

## How It Works (5-Step Flow)

MuhafizSRE uses a fixed five-stage pattern:
- triage and investigation through MCP-informed agents
- safety challenge before approval
- operator-only approval for signed contracts
- bounded execution through typed skills
- hash-chain sealing and verification after recovery

## Architecture

Gateway-orchestrated ADK agents handle all control flow. A deliberate trade-off: explicit deterministic routing preserves one auditable authority boundary that composite orchestrators (SequentialAgent, LoopAgent) would blur. MCP telemetry
feeds investigation inputs, while Muhtasib enforces policy before operator authorization.
Execution is bounded by contract hash, plan hash, and token TTL checks.

---

## Why Agents, Not Scripts?

Traditional runbooks only handle scenarios they were programmed for. When a novel failure emerges — a JWT library deprecation, an unexpected memory leak, a cascade failure across microservices — scripts break. SRE operators must fall back to slow, manual investigation.

AI agents bring **adaptive reasoning**: they diagnose novel failures by cross-referencing multiple data sources, generate remediation strategies that weren't pre-scripted, and explain their reasoning for audit trails. The **multi-agent architecture** adds specialization (each agent excels at one cognitive task) and adversarial safety (Muhtasib actively challenges Mudabbir's plans).

---

## The Innovation: Tamper-Evident Chain of Custody

1. Mudabbir generates a `plan_nonce` — a SHA-256 hash uniquely identifying the remediation plan
2. The nonce travels through safety review → operator authorization → execution
3. Aamil **refuses to execute** unless the nonce matches — preventing plan tampering
4. Every action is appended to an unbroken hash chain: `H(n) = SHA-256(canonical_json(envelope))` where the envelope includes schema_version, incident_id, run_id, sequence, actor, event_type, payload, previous_hash, and created_at

This creates a **tamper-evident** (not tamper-proof) **audit trail** — any modification to the event history breaks the hash chain and is immediately detectable.

---

## Course Concepts Demonstrated

| Key Concept | Where | Evidence |
|-------------|-------|----------|
| **Multi-Agent System (ADK)** | Code: `agents/` | 5 `LlmAgent` instances in a gateway-orchestrated, explicitly routed ADK workflow. Each agent has unique model, tools, and persona. State passed via `tool_context.state` |
| **MCP Server** | Code: `shared/mcp_server/server.py` | `FastMCP` server with 3 enterprise telemetry tools (~2,000 lines). Connected via `MCPToolset` + `StdioConnectionParams` |
| **Google Antigravity** | System architect + AI coding accelerator | Generated HMAC-SHA256 approval boundary boilerplate, Kahn's algorithm topological sorting, cross-file refactoring across 5 agents, test generation (245 tests), iterative security review. Demonstrated in the supporting video — a live read-only security-invariant audit verifying digest-only token storage, constant-time comparison, and envelope hash recomputation: https://www.youtube.com/watch?v=AKLXvlJBLyg |
| **Security Features** | Code: `gateway/store.py`, `gateway/app.py` | SHA-256 hash chain, cryptographic plan nonces, 3-way approval gate, zero hardcoded secrets |
| **Deployability** | Video + Code: `Dockerfile`, `cloudbuild.yaml` | Multi-stage Docker build, non-root user, Cloud Run deployment with Secret Manager |
| **Agent Skills** | Code: `shared/skills.py`, `agents/*.py` | Each agent has a dedicated ADK `FunctionTool` for atomic ledger commits. Aamil executes 6 bounded remediation skills: rollback, rate limit, scale, restart, cache flush, credential rotation. The course also teaches `agents-cli` for scaffolding and lifecycle; MuhafizSRE's bespoke architecture (custom store, ledger, HMAC contracts, pipeline supervisor) does not map onto that template |

---

## Technical Highlights

### MCP Server: Synthetic Telemetry for Repeatable Evaluation (~2,000 Lines)

> **Disclosure**: MuhafizSRE uses **synthetic telemetry** (deterministic fixture data) for repeatable evaluation. The MCP server generates structured data simulating enterprise observability — not connected to a live production environment. This is a deliberate design choice: real production telemetry is non-deterministic and would make evaluation unreproducible.

The MCP server provides three enterprise telemetry tools via `FastMCP`: `get_cloud_logging_traces` (Google Cloud Logging error stacks with trace IDs and Kubernetes pod metadata), `get_github_deployments` (PR merges with commit SHAs and rollback statuses), and `get_system_metrics` (CPU/memory time-series with anomaly spikes and P50/P95/P99 latency percentiles).

### Docker Sandbox: Real State Mutation Within Bounded Scope

> **Disclosure**: The Docker victim service is a **bounded sandbox** — it simulates a microservice health endpoint, not a real production system. However, the mutation is real: Aamil sends a real HTTP POST to a real container, and the health endpoint state changes from 503 → 200. This proves the execution pathway is functional, not simulated.

The sandbox supports two execution modes: `simulated` (deterministic, for benchmarks) and `sandbox` (real HTTP mutation against a Docker victim container). The `sandbox` mode requires both `MUHAFIZ_EXECUTION_MODE=sandbox` and a `VICTIM_SERVICE_URL` — neither alone is sufficient.

### Adversarial Safety: Independent Plan Review
Muhtasib verifies every plan against **15 predefined policy rules** across four categories:
- **CRITICAL** (6 rules): No DROP/TRUNCATE/DELETE, no credential rotation during active P0 unless credentials are root cause, no scaling below 1 replica, rollback only (no new deployments), single-service scope, allowlisted skills and services only
- **HIGH** (4 rules): Rolling restarts during peak need justification, shared cache flushes need downstream impact docs, rate limits below 100 RPS need business justification, sequential same-service actions need ordering rationale
- **EVIDENCE QUALITY** (3 rules): Root cause needs ≥2 data sources, confidence below 0.5 triggers escalation instead of auto-remediation, unknown root cause requires operator escalation
- **REVERSIBILITY** (2 rules): Every action must be reversible, estimated MTTR must be realistic (≥2 min for infrastructure changes)

If any policy is violated, Muhtasib challenges the plan and forces Mudabbir to revise it at a higher thinking level.

### Unified Model Configuration

MuhafizSRE uses a **3-tier cognitive architecture**: `gemini-3.1-flash-lite` for speed-critical tasks (triage, execution), `gemini-3-flash-preview` for analytical reasoning (diagnosis, planning), and `gemini-3.1-pro-preview` for adversarial safety review. Each tier uses calibrated thinking levels (MINIMAL → LOW → MEDIUM → HIGH) matched to task complexity.

---

## The Build: Project Journey

This project began with a core question: *How can we make incident response both faster AND more auditable?* Speed and compliance are typically opposing forces — you either move fast or you document everything. MuhafizSRE resolves this tension through AI agents that inherently generate their own audit trails as they work.

**Key design decisions:**
- **Cognitive tiering** — three model tiers matched to task complexity, with calibrated thinking levels
- **Tamper-evident nonces** over simple approvals — the plan nonce creates a chain of custody that travels from planning through execution
- **Adversarial review** over self-review — a dedicated safety agent challenges the planning agent, catching flaws that self-review would miss
- **MCP over direct API calls** — using the Model Context Protocol for tool integration ensures standards compliance and interoperability
- **Fail closed** — if investigation fails twice, the pipeline halts. No synthetic evidence. No keyword heuristics.

The codebase includes **245+ automated tests** covering agent initialization, MCP tool connectivity, ledger integrity, safety policy validation, skill execution, sandbox fail-closed behavior, architectural invariants, authorization defects, pipeline supervisor, and gateway endpoints.

---

## Setup & Reproduction

**Live deployment**: [https://muhafizsre-851586299411.us-central1.run.app](https://muhafizsre-851586299411.us-central1.run.app) — no setup needed, launch a guided incident directly.

```bash
git clone https://github.com/asadvendor-boop/muhafiz-sre-final.git
cd muhafiz-sre-final
cp .env.example .env   # Add your GEMINI_API_KEY
pip install -e .
adk web agents/        # Launch the ADK interactive UI
```

Full setup instructions, environment variables, and deployment guide available in [README.md](https://github.com/asadvendor-boop/muhafiz-sre-final/blob/main/README.md).

---

## Tech Stack

- **Google ADK** — Multi-agent orchestration (gateway-orchestrated, explicitly routed ADK workflow, LlmAgent)
- **Gemini 3 Flash / 3.1 Pro** — Flash Lite (speed), 3 Flash Preview (analytical), 3.1 Pro Preview (safety)
- **FastMCP** — Model Context Protocol server for synthetic telemetry
- **FastAPI** — Security gateway with 12 API routes
- **Pydantic v2** — Structured data validation (8 models, 4 enums)
- **Next.js 15** — Incident dashboard with Agent Room visualization
- **SQLite** — Audit ledger with SHA-256 hash chain
- **Docker + Cloud Build** — Containerized deployment

**245 Python tests + 35 dashboard frontend tests passing.**


---

## Production Hardening Roadmap

MuhafizSRE is a governed multi-agent SRE prototype with deterministic synthetic enterprise telemetry, real ADK agent workflows, HMAC-bound operator authorization contracts, and real local Docker sandbox remediation. For Fortune-500 production deployment, the next hardening steps are:

1. **Durable orchestration** — Move long-running agent workflows from FastAPI in-process background tasks to Temporal, Cloud Tasks, Celery, or Step Functions.

2. **Backpressure and rate limits** — Add tenant-level concurrency controls, LLM quota management, alert deduplication, priority queues, and budget ceilings.

3. **Production data plane** — Replace SQLite with Postgres/Cloud SQL, add row-level tenant isolation, and anchor terminal event seals to external append-only storage.

4. **Multi-tenant identity** — Add organization_id/workspace_id to incidents, contracts, events, and room messages. Enforce RBAC from authenticated JWT claims.

5. **Real infrastructure adapters** — Replace simulated enterprise skill adapters with parameterized Kubernetes, GCP, Secret Manager, PagerDuty, and ServiceNow integrations.

6. **Resumable execution** — Add idempotent action checkpoints, retry policies, compensation actions, and operator-controlled resume/reissue flows.

7. **Historical context (RAG)** — Vector store of past post-mortems and runbooks for similar-incident retrieval.

**Multi-user demo concurrency:** The live Cloud Run demo is one shared environment. Ledger writes are serialized (process-local `asyncio.Lock`, `BEGIN IMMEDIATE`), and hash chains are isolated per `incident_id` — concurrent reviewers cannot corrupt or cross-contaminate the ledger.

For full transparency on what is production-quality engineering vs. simulated, see [`docs/honest-disclosure.md`](https://github.com/asadvendor-boop/muhafiz-sre-final/blob/main/docs/honest-disclosure.md).

---

*Built for the Kaggle AI Agents: Intensive Vibe Coding Capstone Project with Google*
