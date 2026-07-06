#!/usr/bin/env python3
"""
negative_authority_check.py — Proves REJECT blocks mutation
===========================================================
1. Wait for services healthy
2. Inject fault → victim 503
3. Create incident → pipeline runs
4. Wait for AWAITING_APPROVAL
5. REJECT the contract
6. Confirm incident → REJECTED
7. Confirm victim STILL 503 (no mutation happened)
8. Save proof artifact
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

GATEWAY = "http://localhost:8000"
VICTIM = "http://localhost:9000"
MAX_WAIT_SECS = 240

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

def log_step(step, status, details=""):
    t = time.strftime("%H:%M:%S")
    icon = "✅" if status == "PASS" else "❌"
    print(f"  {icon} Step: {step} → {status}" + (f"  ({details})" if details else ""))
    return {"step": step, "status": status, "time": t, "details": details} if details else {"step": step, "status": status, "time": t}

def main():
    proof = {
        "test": "negative_authority_reject",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "steps": [],
        "status": "RUNNING",
    }

    out_path = os.path.join(os.path.dirname(__file__), "proof_negative_reject.json")

    def save(p):
        with open(out_path, "w") as f:
            json.dump(p, f, indent=2, default=str)

    print("\n" + "=" * 60)
    print("  NEGATIVE AUTHORITY CHECK — REJECT → NO MUTATION")
    print("=" * 60)

    # Wait for services
    print(f"\n⏳ Waiting for services (timeout={MAX_WAIT_SECS}s)")
    start = time.time()
    services_ok = False
    s1, s2 = 0, 0
    while time.time() - start < MAX_WAIT_SECS:
        s1, _ = http_get(f"{GATEWAY}/health")
        s2, _ = http_get(f"{VICTIM}/health")
        if s1 == 200 and s2 == 200:
            services_ok = True
            break
        time.sleep(3)

    if not services_ok:
        proof["steps"].append(log_step("services_healthy", "FAIL", f"gateway={s1}, victim={s2}"))
        proof["status"] = "FAILED"; save(proof); return 1
    proof["steps"].append(log_step("services_healthy", "PASS"))

    # Verify victim healthy
    s, b = http_get(f"{VICTIM}/health")
    if s != 200:
        proof["steps"].append(log_step("victim_healthy", "FAIL", f"status={s}"))
        proof["status"] = "FAILED"; save(proof); return 1
    proof["steps"].append(log_step("victim_healthy", "PASS", f"status={s}"))

    # Inject fault
    s, b = http_post(f"{VICTIM}/inject-fault")
    if s != 200:
        proof["steps"].append(log_step("inject_fault", "FAIL", f"status={s}"))
        proof["status"] = "FAILED"; save(proof); return 1
    proof["steps"].append(log_step("inject_fault", "PASS"))

    # Confirm 503
    s, _ = http_get(f"{VICTIM}/health")
    if s != 503:
        proof["steps"].append(log_step("victim_503", "FAIL", f"expected 503, got {s}"))
        proof["status"] = "FAILED"; save(proof); return 1
    proof["steps"].append(log_step("victim_503", "PASS", f"status={s}"))

    # Create incident
    alert = {
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
    s, b = http_post(f"{GATEWAY}/api/incidents", alert)
    if s != 201:
        proof["steps"].append(log_step("create_incident", "FAIL", f"status={s}"))
        proof["status"] = "FAILED"; save(proof); return 1
    incident_id = b.get("incident_id")
    proof["incident_id"] = incident_id
    proof["steps"].append(log_step("create_incident", "PASS", f"incident_id={incident_id}"))

    # Wait for AWAITING_APPROVAL
    print(f"\n⏳ Waiting for AWAITING_APPROVAL (timeout={MAX_WAIT_SECS}s)")
    start = time.time()
    reached = False
    while time.time() - start < MAX_WAIT_SECS:
        s, b = http_get(f"{GATEWAY}/api/incidents/{incident_id}")
        st = b.get("status", "")
        print(f"    … incident status: {st}")
        if st == "AWAITING_APPROVAL":
            reached = True
            break
        if st in ("FAILED", "ERROR"):
            break
        time.sleep(10)

    if not reached:
        proof["steps"].append(log_step("awaiting_approval", "FAIL", "timeout or error"))
        proof["status"] = "FAILED"; save(proof); return 1
    proof["steps"].append(log_step("awaiting_approval", "PASS", f"took {time.time()-start:.0f}s"))

    # Get contract
    contract = None
    for attempt in range(5):
        s, resp = http_get(f"{GATEWAY}/api/incidents/{incident_id}/contract")
        if s == 200:
            contract = resp.get("contract")
            if contract:
                break
        time.sleep(2)

    if not contract:
        proof["steps"].append(log_step("get_contract", "FAIL", "contract None"))
        proof["status"] = "FAILED"; save(proof); return 1

    token = resp.get("approval_token")
    contract_id = contract.get("contract_id")
    revision = contract.get("revision")
    proof["contract_id"] = contract_id
    proof["steps"].append(log_step("get_contract", "PASS", f"contract_id={contract_id}"))

    # ── REJECT the contract ──
    reject_payload = {
        "action": "REJECT",
        "contract_id": contract_id,
        "revision": revision,
        "approval_token": token,
        "reason": "Negative authority test — human rejects remediation",
    }
    s, b = http_post(f"{GATEWAY}/api/incidents/{incident_id}/decisions", reject_payload)
    if s == 200:
        proof["steps"].append(log_step("reject_contract", "PASS", f"response={b.get('status', b)}"))
    else:
        proof["steps"].append(log_step("reject_contract", "FAIL", f"status={s} body={b}"))
        proof["status"] = "FAILED"; save(proof); return 1

    # Wait briefly for state to settle
    time.sleep(3)

    # Confirm incident status = REJECTED
    s, b = http_get(f"{GATEWAY}/api/incidents/{incident_id}")
    final_status = b.get("status", "")
    if final_status == "REJECTED":
        proof["steps"].append(log_step("incident_rejected", "PASS", f"status={final_status}"))
    else:
        proof["steps"].append(log_step("incident_rejected", "FAIL", f"expected REJECTED, got {final_status}"))
        proof["status"] = "FAILED"; save(proof); return 1

    # ── THE KEY CHECK: victim must STILL be 503 ──
    s, b = http_get(f"{VICTIM}/health")
    if s == 503:
        proof["steps"].append(log_step("victim_still_503", "PASS", f"status={s} — NO MUTATION OCCURRED"))
    else:
        proof["steps"].append(log_step("victim_still_503", "FAIL", f"expected 503, got {s} — UNAUTHORIZED MUTATION!"))
        proof["status"] = "FAILED"; save(proof); return 1

    # Done
    proof["status"] = "PASSED"
    proof["final_incident_status"] = final_status
    save(proof)

    print(f"\n📄 Proof saved: {out_path}")
    print("\n" + "=" * 60)
    print(f"  NEGATIVE AUTHORITY CHECK: PASSED")
    print(f"  Incident: {incident_id} → {final_status}")
    print(f"  Victim still 503: ✅ NO MUTATION WITHOUT APPROVAL")
    print("=" * 60 + "\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
