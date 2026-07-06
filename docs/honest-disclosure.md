# MuhafizSRE — Honest Disclosure

> Transparent disclosure of what is production-quality engineering and what is simulated or aspirational.

---

## Purpose

This document transparently discloses the boundaries between **production-quality engineering** and **simulated/demo components** in MuhafizSRE. Readers evaluating this system — whether judges, colleagues, or future contributors — deserve to know exactly what is real and what is scaffolding.

---

## ✅ What Is Real (Production-Quality)

### 1. Hash-Chained Audit Ledger

| Aspect | Status |
|--------|--------|
| SHA-256 hash chain with proper genesis event | ✅ Production-quality |
| Canonical JSON envelope hash computation | ✅ Production-quality |
| Per-incident chain integrity verification | ✅ Production-quality |
| Seal event with `pre_seal_head_hash` finality | ✅ Production-quality |
| Append-only design (no UPDATE/DELETE on events) | ✅ Production-quality |
| ACID transactions with writer lock | ✅ Production-quality |
| Per-incident chain isolation (concurrent reviewers safe) | ✅ Production-quality |

The hash chain is a **real, functioning cryptographic audit trail**. Any modification to a historical event is detectable via `verify_chain()`. This is not simulated.

---

### 2. HMAC-SHA256 Approval Contracts

| Aspect | Status |
|--------|--------|
| HMAC-SHA256 token generation from canonical JSON claims | ✅ Production-quality |
| Raw token never stored (only SHA-256 digest) | ✅ Production-quality |
| Constant-time comparison via `hmac.compare_digest()` | ✅ Production-quality |
| TTL enforcement on contract expiry | ✅ Production-quality |
| Contract lifecycle with atomic compare-and-set transitions | ✅ Production-quality |
| Plan hash binding (SHA-256 of canonical plan JSON) | ✅ Production-quality |

The cryptographic token flow follows **industry best practices** for HMAC-based token verification. Production deployment would additionally require asymmetric signing and a production-grade datastore.

---

### 3. Multi-Agent Pipeline

| Aspect | Status |
|--------|--------|
| 5 specialized agents with targeted model selection | ✅ Production-quality |
| Routed workflow orchestration via ADK (agent room) | ✅ Production-quality |
| Phase 1/Phase 2 split with async human approval gate | ✅ Production-quality |
| Shared session state for inter-agent communication | ✅ Production-quality |
| Structured output via Pydantic models | ✅ Production-quality |
| Agent-specific tools and personas | ✅ Production-quality |

The agent architecture, prompt engineering, and orchestration logic are real and functional. The Phase 1/Phase 2 split allows asynchronous human-in-the-loop governance without blocking the API server.

---

### 4. Deterministic Action Validation

| Aspect | Status |
|--------|--------|
| Typed argument schemas via Pydantic | ✅ Production-quality |
| Shell metachar / URL / path traversal / command injection regex detection | ✅ Production-quality |
| Target allowlist (`auth-service`, `payment-gateway`, `user-service`) | ✅ Production-quality |
| Skill allowlist (6 enumerated skills) | ✅ Production-quality |
| Forbidden operations blocklist | ✅ Production-quality |
| Acyclic dependency graph validation (Kahn's algorithm) | ✅ Production-quality |
| Topological execution ordering | ✅ Production-quality |

The action validation layer is **entirely deterministic** — no LLM involved. The validation logic is production-quality; the hardcoded allowlist would need dynamic service discovery for production use.

---

### 5. FastAPI Gateway

| Aspect | Status |
|--------|--------|
| RESTful API with proper error handling | ✅ Production-quality |
| SSE streaming for real-time updates | ✅ Production-quality |
| Background task execution | ✅ Production-quality |
| CORS configuration | ✅ Production-quality |
| Health check endpoint | ✅ Production-quality |

---

### 6. Evaluation Framework

| Aspect | Status |
|--------|--------|
| 7 distinct scenarios testing different pipeline paths | ✅ Production-quality |
| Automated metric evaluation with pass/fail grading | ✅ Production-quality |
| Correctness checks for root cause, actions, and terminal states | ✅ Production-quality |
| Chain integrity verification in evaluation | ✅ Production-quality |

---

## ⚠️ What Is Simulated (Not Production-Ready)

### 1. Skill Adapters

| Aspect | Reality |
|--------|---------|
| **6 skill functions** | Real async functions with proper structure |
| **Simulated execution** | `asyncio.sleep(0.05)` — deterministic, no real API calls |
| **Sandbox execution** | Real HTTP POST to Docker victim container (`rollback_service_revision` only) — real local state mutation. Requires `MUHAFIZ_EXECUTION_MODE=sandbox` + `VICTIM_SERVICE_URL`. Not available on Cloud Run |
| **Results** | Simulated: deterministic/hardcoded (e.g., `keys_flushed` from MD5 seed). Sandbox: real HTTP 503→200 recovery |
| **Side effects** | Simulated: zero. Sandbox: real container state mutation (local Docker Compose only) |

**No actual `gcloud`, `kubectl`, or `redis-cli` commands are executed.**

**To make production-ready**: Replace each skill with a real client adapter:
- `rollback_service_revision` → GCP Cloud Run Admin API
- `apply_rate_limit` → API Gateway / Apigee configuration API
- `scale_service` → Cloud Run / Kubernetes HPA API
- `flush_cache` → Redis client (`aioredis`)
- `rotate_credentials` → Secret Manager API
- `restart_service` → Kubernetes API (`kubectl rollout restart`)

---

### 2. MCP Telemetry Server

| Aspect | Reality |
|--------|---------|
| **FastMCP server structure** | Real MCP server with proper tool definitions |
| **Tool responses** | Hardcoded/synthetic data — not real telemetry |

| Tool | What's Simulated |
|------|-----------------|
| `get_cloud_logging_traces` | Returns pre-built log entries with realistic timestamps |
| `get_github_deployments` | Returns simulated deployment history with SHA hashes |
| `get_system_metrics` | Returns simulated time-series (CPU, memory, error rate, latency) |

**To make production-ready**: Connect to real data sources:
- Cloud Logging → GCP Logging API
- Deployments → GitHub REST API / Cloud Build API
- Metrics → Cloud Monitoring API / Prometheus

---

### 3. Recovery Verification

| Aspect | Reality |
|--------|---------|
| **8 subsystem checks** | Real check definitions with realistic names and latencies |
| **Check results** | 7 of 8 always return `healthy` (simulated) |
| **API reachability** | Can do **real HTTP check** if `victim_url` is provided |
| **Latency values** | Hardcoded per check (e.g., 45.2ms for API reachability) |

**To make production-ready**: Implement real probes:
- `api_reachability` → HTTP health endpoint check (already partially real)
- `db_connectivity` → Connection pool ping
- `cache_health` → Redis `PING` command
- `dns_resolution` → `socket.getaddrinfo()` with timing
- `tls_validity` → Certificate expiry check
- `lb_backends` → Cloud Load Balancing API
- `error_rate` → Prometheus `rate(http_requests_total{code=~"5.."}[5m])`
- `p99_latency` → Prometheus `histogram_quantile(0.99, ...)`

---

### 4. Alert Ingestion

| Aspect | Reality |
|--------|---------|
| **Alert format** | Well-defined `Alert` Pydantic model |
| **Ingestion method** | Manual POST to `/api/incidents` or evaluation scenarios |
| **Monitoring integration** | None — no webhook receivers |

**To make production-ready**: Add webhook receivers for:
- PagerDuty → PagerDuty webhook payload parser
- Sentry → Sentry webhook payload parser
- Datadog → Datadog webhook payload parser
- OpsGenie → OpsGenie webhook payload parser
- Prometheus Alertmanager → Alertmanager webhook payload parser

---

### 5. Dashboard

| Aspect | Reality |
|--------|---------|
| **Functionality** | Working Next.js app with SSE timeline and approval UI |
| **Authentication** | ⚠️ None — no login, no RBAC |
| **Hosting** | Local development server only |

**To make production-ready**: Add OAuth/OIDC, role-based access control, and production hosting.

---

## ⛔ Known Limitations

| # | Limitation | Severity | Description |
|---|-----------|----------|-------------|
| 1 | **Single-writer** | Medium | `asyncio.Lock` limits to one concurrent write. Production needs distributed locking (Redis/etcd). |
| 2 | **SQLite** | Medium | Not horizontally scalable. Production would use PostgreSQL with connection pooling. |
| 3 | **No rate limiting** | High | Gateway API has no request rate limiting. Vulnerable to DoS. |
| 4 | **No authentication** | High | API endpoints have no auth (no API keys, OAuth, mTLS). |
| 5 | **No pipeline-level resume** | Medium | Individual agents have retry-with-backoff, but full pipeline failures (e.g., mid-workflow crash) are not automatically resumed or replayed. |
| 6 | **Fixed target allowlist** | Low | Only 3 services hardcoded. Production needs dynamic service discovery. |
| 7 | **No rollback-of-rollback** | Medium | If remediation worsens the incident, there's no automated undo. |
| 8 | **LLM reliability** | Medium | Agent decisions depend on LLM quality; hallucinations possible despite safety review. |
| 9 | **No multi-tenancy** | Low | Single-tenant design. Cannot segregate incidents by team/org. |
| 10 | **No alert deduplication** | Medium | Duplicate alerts create duplicate incidents. |
| 11 | **Recovery verifier scope** | Low | Recovery verification checks are simulated; production would need real health probes. |

---

## 🔄 Architecture Decisions & Trade-offs

| Decision | Rationale | Trade-off |
|----------|-----------|-----------|
| SQLite over PostgreSQL | Zero-config, embedded, perfect for demo and evaluation | Not horizontally scalable, no concurrent writers |
| Simulated skills | Safe for evaluation, no infrastructure required | Cannot validate real remediation effectiveness |
| Routed workflow (agent room) | Deterministic ordering, easier debugging, clear pipeline | No parallel agent execution, higher latency |
| Phase 1/Phase 2 split | Async human governance, non-blocking API | Two separate background tasks to manage |
| HMAC-SHA256 | Standard, well-understood, fast, built into Python | Symmetric key (shared secret vs. asymmetric signing) |
| Flash Lite for triage/execution | Fast, cheap, sufficient for structured tasks | Less capable for complex reasoning |
| Pro for safety review | Most capable model for adversarial analysis | Slower response time, higher cost |
| Single-process architecture | Simplicity, no IPC complexity | Cannot horizontally scale |
| Append-only SQLite ledger | Simple, reliable, no external dependencies | No built-in replication or backup |
| Typed Pydantic schemas | Strong validation, good error messages | Schema evolution requires migration |
| ADK `ToolContext` for state | Native ADK pattern, clean abstraction | Tightly coupled to ADK framework |

---

## 📊 Production Readiness Score

| Category | Score | Notes |
|----------|-------|-------|
| Security Primitives | ⭐⭐⭐⭐⭐ | Hash chain, HMAC, token security follow industry best practices |
| Agent Architecture | ⭐⭐⭐⭐⭐ | Well-designed pipeline with appropriate model selection |
| Action Validation | ⭐⭐⭐⭐⭐ | Deterministic, multi-layer, no LLM dependency |
| API Design | ⭐⭐⭐⭐ | Clean REST + SSE, but missing auth and rate limiting |
| Persistence | ⭐⭐⭐ | Correct but uses SQLite (not scalable) |
| Skill Execution | ⭐⭐ | Properly structured but fully simulated |
| Monitoring Integration | ⭐ | No real monitoring platform integration |
| Operational Readiness | ⭐⭐ | Docker + Cloud Run config, but no observability |

---

## 🔮 Planned Safety Upgrades

The following improvements are identified but intentionally deferred to avoid destabilizing the final submission:

1. **Synthetic safety finalizer actor attribution** — When the gateway's fallback safety finalizer writes a `safety_review_completed` event (because Muhtasib's output could not be parsed), the `actor` field should be `gateway_safety_guard`, not `muhtasib`. The current attribution incorrectly implies the agent itself signed the decision. Fix: one-line actor rename in the gateway fallback path.

2. **Event actor provenance enforcement** — Extend the event schema to distinguish between `agent_actor` (LLM-generated decision) and `system_actor` (gateway/deterministic fallback), making the provenance of every event machine-verifiable.

3. **Audit seal external anchoring** — Terminal event seals are currently stored in the same SQLite database as the events they seal. Production deployment should anchor seals to external append-only storage for true tamper-evidence.

---

## Production Hardening Roadmap

MuhafizSRE is a governed multi-agent SRE prototype with deterministic synthetic enterprise telemetry, real ADK agent workflows, HMAC-bound human approval contracts, and real local Docker sandbox remediation. For Fortune-500 production deployment, the next hardening steps are:

1. **Durable orchestration** — Move long-running agent workflows from FastAPI in-process background tasks to Temporal, Cloud Tasks, Celery, or Step Functions.
2. **Backpressure and rate limits** — Add tenant-level concurrency controls, LLM quota management, alert deduplication, priority queues, and budget ceilings.
3. **Production data plane** — Replace SQLite with Postgres/Cloud SQL, add row-level tenant isolation, and anchor terminal event seals to external append-only storage.
4. **Multi-tenant identity** — Add organization_id/workspace_id to incidents, contracts, events, and room messages. Enforce RBAC from authenticated JWT claims.
5. **Real infrastructure adapters** — Replace simulated enterprise skill adapters with parameterized Kubernetes, GCP, Secret Manager, PagerDuty, and ServiceNow integrations.
6. **Resumable execution** — Add idempotent action checkpoints, retry policies, compensation actions, and operator-controlled resume/reissue flows.

