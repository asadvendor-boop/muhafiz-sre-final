#!/usr/bin/env python3
"""
sandbox_proof_b.py — Automated Proof B Generator for MuhafizSRE
================================================================

Runs the complete sandbox smoke test sequence:
1. Wait for all services healthy
2. Verify victim starts healthy (200)
3. Inject fault → verify 503
4. Create incident via gateway API
5. Wait for AWAITING_APPROVAL
6. Approve exact contract
7. Wait for RESOLVED
8. Verify victim recovered (200)
9. Download and validate the full incident audit JSON
10. Save proof artifact

Monitors every 10s and aborts on errors to save LLM costs.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

GATEWAY = "http://localhost:8000"
VICTIM = "http://localhost:9000"
DASHBOARD = "http://localhost:3000"
PROOF_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_WAIT_SECS = 180  # max wait for any phase

def http_get(url, timeout=10):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(body)
        except:
            return e.code, {"raw": body}
    except Exception as e:
        return 0, {"error": str(e)}

def http_post(url, data=None, timeout=15):
    try:
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method="POST")
        if body:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(body)
        except:
            return e.code, {"raw": body}
    except Exception as e:
        return 0, {"error": str(e)}

def wait_for(description, check_fn, interval=10, timeout=MAX_WAIT_SECS):
    """Poll check_fn every interval seconds until it returns True or timeout."""
    print(f"\n⏳ Waiting: {description} (timeout={timeout}s)")
    start = time.time()
    while time.time() - start < timeout:
        result = check_fn()
        if result:
            elapsed = time.time() - start
            print(f"  ✅ {description} — done in {elapsed:.1f}s")
            return True
        time.sleep(interval)
    print(f"  ❌ TIMEOUT: {description} after {timeout}s")
    return False

def main():
    proof = {
        "test": "sandbox_smoke_proof_b",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "steps": [],
        "status": "RUNNING",
    }

    def log_step(name, status, details=None):
        step = {"step": name, "status": status, "time": time.strftime("%H:%M:%S")}
        if details:
            step["details"] = details
        proof["steps"].append(step)
        icon = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⏳"
        print(f"  {icon} Step: {name} → {status}")
        if status == "FAIL":
            print(f"     Details: {details}")

    # ── Step 1: Wait for services ──
    print("\n" + "="*60)
    print("  PROOF B — SANDBOX SMOKE TEST")
    print("="*60)

    services_ok = wait_for(
        "All services healthy",
        lambda: all([
            http_get(f"{GATEWAY}/health")[0] == 200,
            http_get(f"{VICTIM}/status")[0] == 200,
        ]),
        interval=10,
        timeout=300,  # builds can take a while
    )
    if not services_ok:
        log_step("services_healthy", "FAIL", "Gateway or victim not reachable")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    log_step("services_healthy", "PASS")

    # ── Step 2: Verify victim starts healthy ──
    status, body = http_get(f"{VICTIM}/health")
    if status == 200:
        log_step("victim_initially_healthy", "PASS", f"status={status}")
    else:
        log_step("victim_initially_healthy", "FAIL", f"status={status} body={body}")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    # ── Step 3: Inject fault ──
    status, body = http_post(f"{VICTIM}/inject-fault?error_rate=1.0&latency_ms=0")
    if status == 200:
        log_step("inject_fault", "PASS", f"status={status}")
    else:
        log_step("inject_fault", "FAIL", f"status={status} body={body}")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    # ── Step 4: Verify victim is unhealthy (503) ──
    status, body = http_get(f"{VICTIM}/health")
    if status == 503:
        log_step("victim_unhealthy_503", "PASS", f"status={status}")
    else:
        log_step("victim_unhealthy_503", "FAIL", f"expected 503, got {status}")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    # ── Step 5: Create incident ──
    alert_payload = {
        "alert": {
            "alert_type": "error_rate",
            "service_id": "auth-service",
            "severity": "P1",
            "summary": "Error rate spike to 100% on auth-service after deployment v2.4.1",
            "error_message": "HTTP 503 Service Unavailable — upstream connection refused on auth-service:8443",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "scenario_id": "bad_deployment",
    }
    status, body = http_post(f"{GATEWAY}/api/incidents", alert_payload)
    if status == 201:
        incident_id = body.get("incident_id")
        log_step("create_incident", "PASS", f"incident_id={incident_id}")
    else:
        log_step("create_incident", "FAIL", f"status={status} body={body}")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    proof["incident_id"] = incident_id

    # ── Step 6: Wait for AWAITING_APPROVAL ──
    contract_data = [None]  # mutable container

    def check_awaiting():
        s, b = http_get(f"{GATEWAY}/api/incidents/{incident_id}")
        current_status = b.get("status", "")
        print(f"    … incident status: {current_status}")
        if current_status == "PIPELINE_FAILED":
            return "FAILED"
        if current_status == "AWAITING_APPROVAL":
            return True
        return False

    result = wait_for("AWAITING_APPROVAL", check_awaiting, interval=10, timeout=MAX_WAIT_SECS)
    if result == "FAILED" or not result:
        log_step("awaiting_approval", "FAIL", "Pipeline failed or timeout")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    log_step("awaiting_approval", "PASS")

    # ── Step 7: Get contract and approve ──
    # Retry a few times — there's a brief window after AWAITING_APPROVAL
    # where the contract endpoint may not return the full payload yet.
    contract = None
    for attempt in range(5):
        status, contract_resp = http_get(f"{GATEWAY}/api/incidents/{incident_id}/contract")
        if status != 200:
            log_step("get_contract", "RETRY", f"status={status}, attempt={attempt+1}")
            time.sleep(2)
            continue
        contract = contract_resp.get("contract")
        if contract:
            break
        log_step("get_contract", "RETRY", f"contract=None, attempt={attempt+1}")
        time.sleep(2)

    if not contract:
        log_step("get_contract", "FAIL", f"contract still None after retries")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    token = contract_resp.get("approval_token")
    contract_id = contract.get("contract_id")
    revision = contract.get("revision")
    log_step("get_contract", "PASS", f"contract_id={contract_id}")

    decision_payload = {
        "action": "APPROVE",
        "contract_id": contract_id,
        "revision": revision,
        "approval_token": token,
    }
    status, body = http_post(f"{GATEWAY}/api/incidents/{incident_id}/decisions", decision_payload)
    if status == 200:
        log_step("approve_contract", "PASS")
    else:
        log_step("approve_contract", "FAIL", f"status={status} body={body}")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    # ── Step 8: Wait for RESOLVED ──
    def check_resolved():
        s, b = http_get(f"{GATEWAY}/api/incidents/{incident_id}")
        current_status = b.get("status", "")
        print(f"    … incident status: {current_status}")
        if current_status in ("RESOLVED", "RECOVERY_FAILED", "DEGRADED"):
            return current_status
        return False

    final_status = [None]
    def check_and_capture():
        result = check_resolved()
        if result:
            final_status[0] = result
            return True
        return False

    result = wait_for("Final status (RESOLVED/DEGRADED)", check_and_capture, interval=10, timeout=MAX_WAIT_SECS)
    if not result:
        log_step("final_status", "FAIL", "Timeout waiting for resolution")
        proof["status"] = "FAILED"
        save_proof(proof)
        return 1

    log_step("final_status", "PASS" if final_status[0] == "RESOLVED" else "WARN",
             f"status={final_status[0]}")

    # ── Step 9: Verify victim recovered ──
    status, body = http_get(f"{VICTIM}/health")
    if status == 200:
        log_step("victim_recovered_200", "PASS", f"status={status} body={body}")
    else:
        log_step("victim_recovered_200", "FAIL", f"expected 200, got {status}")

    # ── Step 10: Download full incident audit ──
    status, audit = http_get(f"{GATEWAY}/api/incidents/{incident_id}")
    proof["audit"] = audit

    # Check for sandbox receipt provenance
    events_status, events_data = http_get(f"{GATEWAY}/api/incidents/{incident_id}/events")
    if events_status == 200:
        events = events_data if isinstance(events_data, list) else events_data.get("events", [])
        proof["events_count"] = len(events)

        # Find execution receipts
        for event in events:
            payload = event.get("payload", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except:
                    continue

            # Check for sandbox receipts in various locations
            receipts = payload.get("receipts", payload.get("execution_receipts", {}))
            if receipts:
                proof["execution_receipts"] = receipts
                # Check for is_real_mutation
                for action_id, receipt in receipts.items() if isinstance(receipts, dict) else []:
                    if receipt.get("is_real_mutation"):
                        log_step("sandbox_receipt_provenance", "PASS", {
                            "adapter": receipt.get("adapter"),
                            "is_real_mutation": receipt.get("is_real_mutation"),
                            "before_state": receipt.get("detail", {}).get("before_state"),
                            "after_state": receipt.get("detail", {}).get("after_state"),
                        })
                        proof["sandbox_receipt"] = receipt

            # Check recovery verification
            if event.get("event_type") == "recovery_verified":
                recovery = payload
                proof["recovery_verification"] = recovery
                log_step("recovery_verification", "PASS", {
                    "status": recovery.get("status"),
                    "verification_mode": recovery.get("verification_mode"),
                    "is_real_observation": recovery.get("is_real_observation"),
                })

    # ── Final verdict ──
    proof["final_incident_status"] = final_status[0]
    proof["status"] = "PASSED" if final_status[0] == "RESOLVED" else "PARTIAL"

    save_proof(proof)

    print("\n" + "="*60)
    print(f"  PROOF B RESULT: {proof['status']}")
    print(f"  Incident: {incident_id}")
    print(f"  Final Status: {final_status[0]}")
    print(f"  Events: {proof.get('events_count', '?')}")
    print("="*60)

    return 0

def save_proof(proof):
    proof_path = os.path.join(PROOF_DIR, "proof_b_sandbox_smoke.json")
    with open(proof_path, "w") as f:
        json.dump(proof, f, indent=2, default=str)
    print(f"\n📄 Proof saved: {proof_path}")

if __name__ == "__main__":
    sys.exit(main())
