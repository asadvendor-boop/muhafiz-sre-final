#!/usr/bin/env python3
"""
negative_authority_matrix.py — Extended negative authority checks
================================================================
Tests the authority boundary under adversarial conditions:
  1. Tampered token       → 403, victim stays 503
  2. Tampered revision    → 409, victim stays 503
  3. Duplicate approval   → second call rejected, only one mutation
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
    icon = "✅" if status == "PASS" else "❌"
    print(f"  {icon} {step}: {status}" + (f"  ({details})" if details else ""))
    return {"step": step, "status": status, "details": details}


def create_incident_and_wait():
    """Inject fault, create incident, wait for AWAITING_APPROVAL, return contract info."""
    # Inject fault
    http_post(f"{VICTIM}/inject-fault")
    s, _ = http_get(f"{VICTIM}/health")
    if s != 503:
        return None, None, "victim not 503 after fault injection"

    alert = {
        "alert": {
            "alert_type": "error_rate",
            "service_id": "auth-service",
            "severity": "P1",
            "summary": "Error rate spike to 100% on auth-service after deployment v2.4.1",
            "error_message": "HTTP 503 Service Unavailable",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "scenario_id": "bad_deployment",
    }
    s, b = http_post(f"{GATEWAY}/api/incidents", alert)
    if s != 201:
        return None, None, f"create incident failed: {s}"
    incident_id = b["incident_id"]

    # Wait for AWAITING_APPROVAL
    start = time.time()
    while time.time() - start < MAX_WAIT_SECS:
        s, b = http_get(f"{GATEWAY}/api/incidents/{incident_id}")
        st = b.get("status", "")
        print(f"    … {st}")
        if st == "AWAITING_APPROVAL":
            break
        if st in ("FAILED", "ERROR"):
            return incident_id, None, f"pipeline failed: {st}"
        time.sleep(10)
    else:
        return incident_id, None, "timeout waiting for AWAITING_APPROVAL"

    # Get contract
    for _ in range(5):
        s, resp = http_get(f"{GATEWAY}/api/incidents/{incident_id}/contract")
        if s == 200 and resp.get("contract"):
            return incident_id, resp, None
        time.sleep(2)
    return incident_id, None, "contract not found"


def test_tampered_token(incident_id, contract_resp):
    """Submit approval with a tampered HMAC token → expect 403."""
    print("\n── Test: Tampered Token ──")
    contract = contract_resp["contract"]
    payload = {
        "action": "APPROVE",
        "contract_id": contract["contract_id"],
        "revision": contract["revision"],
        "approval_token": "TAMPERED_FAKE_TOKEN_12345",
    }
    s, b = http_post(f"{GATEWAY}/api/incidents/{incident_id}/decisions", payload)
    if s == 403:
        step = log_step("tampered_token_rejected", "PASS", f"status={s}")
    else:
        step = log_step("tampered_token_rejected", "FAIL", f"expected 403, got {s} body={b}")

    # Victim must still be 503
    vs, _ = http_get(f"{VICTIM}/health")
    if vs == 503:
        step2 = log_step("victim_still_503_after_tamper", "PASS", f"status={vs}")
    else:
        step2 = log_step("victim_still_503_after_tamper", "FAIL", f"expected 503, got {vs}")
    return [step, step2]


def test_tampered_revision(incident_id, contract_resp):
    """Submit approval with wrong revision → expect 409."""
    print("\n── Test: Tampered Revision ──")
    contract = contract_resp["contract"]
    token = contract_resp.get("approval_token")
    payload = {
        "action": "APPROVE",
        "contract_id": contract["contract_id"],
        "revision": 9999,  # wrong revision
        "approval_token": token,
    }
    s, b = http_post(f"{GATEWAY}/api/incidents/{incident_id}/decisions", payload)
    if s in (409, 400, 403):
        step = log_step("tampered_revision_rejected", "PASS", f"status={s}")
    else:
        step = log_step("tampered_revision_rejected", "FAIL", f"expected 409/400/403, got {s} body={b}")

    # Victim must still be 503
    vs, _ = http_get(f"{VICTIM}/health")
    if vs == 503:
        step2 = log_step("victim_still_503_after_revision_tamper", "PASS", f"status={vs}")
    else:
        step2 = log_step("victim_still_503_after_revision_tamper", "FAIL", f"expected 503, got {vs}")
    return [step, step2]


def test_duplicate_approval(incident_id, contract_resp):
    """Submit valid approval twice → first succeeds, second rejected."""
    print("\n── Test: Duplicate Approval ──")
    contract = contract_resp["contract"]
    token = contract_resp.get("approval_token")
    payload = {
        "action": "APPROVE",
        "contract_id": contract["contract_id"],
        "revision": contract["revision"],
        "approval_token": token,
    }

    # First approval — should succeed
    s1, b1 = http_post(f"{GATEWAY}/api/incidents/{incident_id}/decisions", payload)
    if s1 == 200:
        step1 = log_step("first_approval_accepted", "PASS", f"status={s1}")
    else:
        step1 = log_step("first_approval_accepted", "FAIL", f"expected 200, got {s1}")
        return [step1]

    # Wait for execution to complete
    time.sleep(5)

    # Second approval with same token — should be rejected
    s2, b2 = http_post(f"{GATEWAY}/api/incidents/{incident_id}/decisions", payload)
    if s2 in (409, 400, 403):
        step2 = log_step("duplicate_approval_rejected", "PASS", f"status={s2}")
    elif s2 == 200 and b2.get("status") in ("already_resolved", "already_approved"):
        step2 = log_step("duplicate_approval_rejected", "PASS", f"status={s2} (idempotent)")
    else:
        step2 = log_step("duplicate_approval_rejected", "FAIL", f"expected rejection, got {s2} body={b2}")

    return [step1, step2]


def main():
    proof = {
        "test": "negative_authority_matrix",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tests": [],
        "status": "RUNNING",
    }
    out_path = os.path.join(os.path.dirname(__file__), "proof_negative_matrix.json")

    print("\n" + "=" * 60)
    print("  NEGATIVE AUTHORITY MATRIX")
    print("=" * 60)

    # Wait for services
    print("⏳ Waiting for services...")
    start = time.time()
    while time.time() - start < 60:
        s1, _ = http_get(f"{GATEWAY}/health")
        s2, _ = http_get(f"{VICTIM}/health")
        if s1 == 200 and s2 == 200:
            break
        time.sleep(3)

    # === Test 1 & 2: Tampered token + tampered revision ===
    # These use the SAME incident — we tamper then the contract stays valid
    print("\n🔧 Creating incident for tamper tests...")
    inc_id, contract_resp, err = create_incident_and_wait()
    if err:
        print(f"  ❌ Setup failed: {err}")
        proof["status"] = "SETUP_FAILED"
        proof["error"] = err
        with open(out_path, "w") as f:
            json.dump(proof, f, indent=2)
        return 1

    # Test 1: Tampered token
    results = test_tampered_token(inc_id, contract_resp)
    proof["tests"].append({"name": "tampered_token", "steps": results})

    # Test 2: Tampered revision
    results = test_tampered_revision(inc_id, contract_resp)
    proof["tests"].append({"name": "tampered_revision", "steps": results})

    # Test 3: Duplicate approval (use same incident — first approval will succeed)
    results = test_duplicate_approval(inc_id, contract_resp)
    proof["tests"].append({"name": "duplicate_approval", "steps": results})

    # Determine overall status
    all_steps = [s for t in proof["tests"] for s in t["steps"]]
    all_passed = all(s["status"] == "PASS" for s in all_steps)
    proof["status"] = "PASSED" if all_passed else "FAILED"
    proof["incident_id"] = inc_id

    with open(out_path, "w") as f:
        json.dump(proof, f, indent=2)

    print(f"\n📄 Proof saved: {out_path}")
    print("\n" + "=" * 60)
    total = len(all_steps)
    passed = sum(1 for s in all_steps if s["status"] == "PASS")
    print(f"  NEGATIVE AUTHORITY MATRIX: {'PASSED' if all_passed else 'FAILED'} ({passed}/{total})")
    print("=" * 60 + "\n")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
