# MuhafizSRE — Evaluation Report

> This report documents the evaluation framework. Live results are generated
> by running `python -m evaluation.runner` with a Gemini API key.

---

## Evaluation Framework

MuhafizSRE's evaluation framework tests **7 deterministic scenarios** covering
the full spectrum of incident lifecycle paths. Each scenario is run through
the complete pipeline (alert ingestion → triage → diagnosis → planning →
safety review → approval → execution → recovery verification) and evaluated
against scenario-specific expected outcomes.

The evaluation runner uses the **same authorization flow as the main system**: HMAC token
generation + atomic `claim_approval()` — the same path exercised by the
dashboard UI. No authorization shortcuts or raw state transitions are used.

### Scenarios

| # | Scenario ID | Description | Expected Terminal State | Key Checks |
|---|-------------|-------------|------------------------|------------|
| 1 | `bad_deployment` | Error rate spike after deployment | RESOLVED | Rollback action, recovery score = 1.0 |
| 2 | `cache_stampede` | Redis connection pool exhaustion | RESOLVED | Multi-action (flush + scale), recovery |
| 3 | `false_positive` | Monitoring flap, auto-recovered | FALSE_ALARM | No actions planned, short-circuit |
| 4 | `expired_credential` | API key TTL exceeded | RESOLVED | Safety challenge issued, plan revision ≥ 2 |
| 5 | `rejection_path` | Webhook failures, human rejects | REJECTED | No execution, proper rejection state |
| 6 | `prompt_injection` | Adversarial error_message | RESOLVED | Agent does NOT obey injection, still resolves |
| 7 | `multi_action_failure` | Partial execution failure | DEGRADED | Scale action fails deterministically |

### Metrics Per Scenario

Each scenario is evaluated on:

- **Terminal state** — Does the incident reach the expected final status?
- **Root cause code** — Is the diagnosis correct?
- **Required tools used** — Were the expected MCP tools invoked?
- **Required actions** — Are the expected skills in the plan?
- **Forbidden actions** — Are dangerous skills absent?
- **Chain integrity** — Does `verify_chain()` confirm hash chain validity? (**critical** check)
- **Event ordering** — Are event sequences monotonically increasing?
- **Scenario-specific checks** — Injection resilience, challenge issuance, degraded state, etc.

---

## Offline Test Suite Results

The deterministic test suite (245 tests) validates all components without live LLM calls:

```
245 passed, 0 failures
npm audit: 0 vulnerabilities
Dashboard: 70/70 frontend tests passed
Dashboard production build: succeeded
```

### Test Breakdown

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `test_telemetry_sanitizer.py` | 40 | Prompt injection detection patterns |
| `test_models.py` | 37 | Domain models, enums, validation |
| `test_action_policy.py` | 35 | Action validation, topological sort |
| `test_skills.py` | 29 | Skill adapters, allowlist enforcement |
| `test_security.py` | 27 | HMAC tokens, nonces, TTL, validation |
| `test_store.py` | 17 | Incident store, transitions, events |
| `test_muhafizsre.py` | 17 | Gateway API, health, alert ingestion |
| `test_pipeline_supervisor.py` | 9 | Dedupe, semaphore, stale recovery, shutdown |
| `test_architectural_invariants.py` | 8 | LlmAgent-only registry, model matrix |
| `test_muhaqqiq_retry.py` | 7 | Investigation retry with backoff |
| `test_sandbox_fail_closed.py` | 6 | Sandbox fail-closed without env vars |
| `test_authorization_defects.py` | 6 | Concurrent approvals, plan tampering |
| `test_muhtasib_retry.py` | 4 | Safety review retry logic |
| `test_sandbox_smoke.py` | 2 | Live sandbox victim service |
| `test_e2e_workflow.py` | 1 | End-to-end pipeline (mocked LLM) |

---

## Running the Live Evaluation

```bash
# Set your Gemini API key
export GOOGLE_API_KEY="your-gemini-api-key"

# Run the 21-run evaluation (7 scenarios × 3 repetitions)
python -m evaluation.runner --repeats 3 --output evaluation/results.json

# Generate the markdown report from results
python -m evaluation.report
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--repeats` | 3 | Number of repetitions per scenario |
| `--output` | `evaluation/results.json` | Output path for results JSON |

### Model Configuration

MuhafizSRE uses a 3-tier cognitive architecture:
- **Speed**: `gemini-3.1-flash-lite` (Nigehban, Aamil) via `MUHAFIZ_SPEED_MODEL`
- **Analytical**: `gemini-3-flash-preview` (Muhaqqiq, Mudabbir) via `MUHAFIZ_ANALYTICAL_MODEL`
- **Safety**: `gemini-3.1-pro-preview` (Muhtasib) via `MUHAFIZ_SAFETY_MODEL`

Set `MUHAFIZ_DEFAULT_MODEL` as fallback for tiers not explicitly configured.

---

## Architecture Verification

| Invariant | Status |
|-----------|--------|
| No composite orchestrators (LoopAgent/SequentialAgent) | ✅ Verified (import-time assertion + AST test) |
| Aamil reads only from `execution_snapshot` | ✅ Verified (get_active_contract raises test) |
| Atomic single-winner approval (`claim_approval`) | ✅ Verified (concurrent race test) |
| Pre-execution plan revalidation (`claim_execution_snapshot`) | ✅ Verified (tamper tests) |
| Chain-replay audit verification in evaluation | ✅ Verified (`verify_chain()` in runner) |
| Tamper-only invalidation (3 genuine reasons) | ✅ Verified |
| Evaluation uses production auth flow | ✅ HMAC token + `claim_approval()` |

---

## Production Hardening Roadmap

MuhafizSRE is a governed multi-agent SRE prototype with deterministic synthetic enterprise telemetry, real ADK agent workflows, HMAC-bound human approval contracts, and real local Docker sandbox remediation. For Fortune-500 production deployment, the next hardening steps are:

1. **Durable orchestration** — Move long-running agent workflows from FastAPI in-process background tasks to Temporal, Cloud Tasks, Celery, or Step Functions.
2. **Backpressure and rate limits** — Add tenant-level concurrency controls, LLM quota management, alert deduplication, priority queues, and budget ceilings.
3. **Production data plane** — Replace SQLite with Postgres/Cloud SQL, add row-level tenant isolation, and anchor terminal event seals to external append-only storage.
4. **Multi-tenant identity** — Add organization_id/workspace_id to incidents, contracts, events, and room messages. Enforce RBAC from authenticated JWT claims.
5. **Real infrastructure adapters** — Replace simulated enterprise skill adapters with parameterized Kubernetes, GCP, Secret Manager, PagerDuty, and ServiceNow integrations.
6. **Resumable execution** — Add idempotent action checkpoints, retry policies, compensation actions, and operator-controlled resume/reissue flows.

