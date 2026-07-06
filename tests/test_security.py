"""
tests/test_security.py – Unit Tests for Approval Token Security
=====================================================================

Tests HMAC-SHA256 token lifecycle:
    - Token generation and verification
    - Token digest (SHA-256 of token, for DB storage)
    - Token expiry
    - Claims validation
    - Full decision request validation
"""

import time

import pytest

from gateway.security import (
    ApprovalTokenManager,
    Settings,
    build_token_claims,
    generate_approval_nonce,
    is_token_expired,
    validate_decision_request,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def secret():
    """A test secret of sufficient length."""
    return "test-secret-that-is-at-least-32-characters-long"


@pytest.fixture
def token_mgr(secret):
    """Create an ApprovalTokenManager for testing."""
    return ApprovalTokenManager(secret)


@pytest.fixture
def sample_claims():
    """Create sample token claims."""
    return build_token_claims(
        incident_id="INC-TEST-001",
        contract_id="CTR-TEST-001",
        revision=1,
        plan_hash="abc123hash",
        nonce="nonce123",
        ttl_seconds=600,
    )


# ── Test: Settings ────────────────────────────────────────────────────────────

def test_settings_from_env_defaults():
    """Test Settings with default values."""
    settings = Settings.from_env()
    assert settings.default_model == "gemini-3.1-flash-lite"
    # With no tier env vars set, each tier resolves to its own default
    assert settings.speed_model == "gemini-3.1-flash-lite"
    assert settings.analytical_model == "gemini-3-flash-preview"
    assert settings.safety_model == "gemini-3.1-pro-preview"
    assert settings.token_ttl == 600


def test_settings_validate_fails_without_secret():
    """Test settings validation fails without approval secret."""
    settings = Settings(approval_secret="short")
    with pytest.raises(ValueError, match="MUHAFIZ_APPROVAL_SECRET"):
        settings.validate()


def test_settings_validate_passes_in_test_mode():
    """Test settings validation passes in test mode."""
    settings = Settings(approval_secret="short", test_mode=True)
    # Should not raise
    settings.validate()


# ── Test: Token Manager ──────────────────────────────────────────────────────

def test_token_manager_creation(secret):
    """Test creating a token manager."""
    mgr = ApprovalTokenManager(secret)
    assert mgr is not None


def test_token_manager_short_secret_raises():
    """Test that a short secret raises ValueError."""
    with pytest.raises(ValueError, match="at least 32 characters"):
        ApprovalTokenManager("short")


def test_generate_token(token_mgr, sample_claims):
    """Test token generation returns a hex string."""
    token = token_mgr.generate_token(sample_claims)
    assert isinstance(token, str)
    assert len(token) == 64  # SHA-256 hex = 64 chars


def test_verify_token_valid(token_mgr, sample_claims):
    """Test verification succeeds for valid token."""
    token = token_mgr.generate_token(sample_claims)
    assert token_mgr.verify_token(token, sample_claims) is True


def test_verify_token_invalid(token_mgr, sample_claims):
    """Test verification fails for invalid token."""
    assert token_mgr.verify_token("invalid_token", sample_claims) is False


def test_verify_token_wrong_claims(token_mgr, sample_claims):
    """Test verification fails with different claims."""
    token = token_mgr.generate_token(sample_claims)
    wrong_claims = sample_claims.copy()
    wrong_claims["incident_id"] = "INC-WRONG"
    assert token_mgr.verify_token(token, wrong_claims) is False


def test_different_secrets_produce_different_tokens(sample_claims):
    """Test that different secrets produce different tokens."""
    mgr1 = ApprovalTokenManager("secret-one-that-is-at-least-32-chars")
    mgr2 = ApprovalTokenManager("secret-two-that-is-at-least-32-chars")
    token1 = mgr1.generate_token(sample_claims)
    token2 = mgr2.generate_token(sample_claims)
    assert token1 != token2


def test_token_deterministic(token_mgr, sample_claims):
    """Test that the same claims produce the same token."""
    token1 = token_mgr.generate_token(sample_claims)
    token2 = token_mgr.generate_token(sample_claims)
    assert token1 == token2


# ── Test: Token Digest ───────────────────────────────────────────────────────

def test_token_digest(token_mgr, sample_claims):
    """Test token digest produces a SHA-256 hex string."""
    token = token_mgr.generate_token(sample_claims)
    digest = ApprovalTokenManager.token_digest(token)
    assert isinstance(digest, str)
    assert len(digest) == 64


def test_token_digest_deterministic(token_mgr, sample_claims):
    """Test digest is deterministic."""
    token = token_mgr.generate_token(sample_claims)
    d1 = ApprovalTokenManager.token_digest(token)
    d2 = ApprovalTokenManager.token_digest(token)
    assert d1 == d2


def test_token_digest_differs_from_token(token_mgr, sample_claims):
    """Test that digest differs from the raw token."""
    token = token_mgr.generate_token(sample_claims)
    digest = ApprovalTokenManager.token_digest(token)
    assert token != digest


# ── Test: Token Reconstruction ───────────────────────────────────────────────

def test_reconstruct_token(token_mgr, sample_claims):
    """Test token reconstruction produces same token as generate."""
    token = token_mgr.generate_token(sample_claims)
    reconstructed = token_mgr.reconstruct_token(sample_claims)
    assert token == reconstructed


# ── Test: Nonce Generation ───────────────────────────────────────────────────

def test_generate_approval_nonce():
    """Test nonce generation produces a 64-char hex string."""
    nonce = generate_approval_nonce()
    assert isinstance(nonce, str)
    assert len(nonce) == 64  # 32 bytes = 64 hex chars


def test_nonce_uniqueness():
    """Test that successive nonces are unique."""
    nonces = {generate_approval_nonce() for _ in range(100)}
    assert len(nonces) == 100


# ── Test: Claims Builder ─────────────────────────────────────────────────────

def test_build_token_claims():
    """Test claims builder produces correct structure."""
    claims = build_token_claims(
        incident_id="INC-001",
        contract_id="CTR-001",
        revision=2,
        plan_hash="hashXYZ",
        nonce="nonce456",
        ttl_seconds=300,
    )

    assert claims["incident_id"] == "INC-001"
    assert claims["contract_id"] == "CTR-001"
    assert claims["revision"] == 2
    assert claims["plan_hash"] == "hashXYZ"
    assert claims["nonce"] == "nonce456"
    assert "exp" in claims
    assert claims["exp"] > time.time()


# ── Test: Token Expiry ───────────────────────────────────────────────────────

def test_is_token_expired_not_expired():
    """Test non-expired claims."""
    claims = {"exp": int(time.time()) + 600}
    assert is_token_expired(claims) is False


def test_is_token_expired_is_expired():
    """Test expired claims."""
    claims = {"exp": int(time.time()) - 100}
    assert is_token_expired(claims) is True


def test_is_token_expired_no_exp():
    """Test missing exp field treated as expired."""
    assert is_token_expired({}) is True


# ── Test: Full Decision Validation ───────────────────────────────────────────

def test_validate_decision_valid(token_mgr):
    """Test full validation succeeds for valid request."""
    nonce = generate_approval_nonce()
    claims = build_token_claims(
        incident_id="INC-V-001",
        contract_id="CTR-V-001",
        revision=1,
        plan_hash="plan_hash_valid",
        nonce=nonce,
    )
    token = token_mgr.generate_token(claims)
    digest = ApprovalTokenManager.token_digest(token)

    contract = {
        "contract_id": "CTR-V-001",
        "incident_id": "INC-V-001",
        "revision": 1,
        "plan_hash": "plan_hash_valid",
        "approval_nonce": nonce,
        "token_digest": digest,
        "status": "ISSUED",
    }

    incident = {
        "incident_id": "INC-V-001",
        "status": "AWAITING_APPROVAL",
    }

    valid, error = validate_decision_request(
        token_manager=token_mgr,
        token=token,
        claims=claims,
        contract=contract,
        incident=incident,
    )

    assert valid is True
    assert error == ""


def test_validate_decision_invalid_signature(token_mgr):
    """Test validation fails with wrong token."""
    claims = build_token_claims(
        incident_id="INC-V-002",
        contract_id="CTR-V-002",
        revision=1,
        plan_hash="hash",
        nonce="nonce",
    )

    contract = {
        "contract_id": "CTR-V-002",
        "incident_id": "INC-V-002",
        "revision": 1,
        "plan_hash": "hash",
        "approval_nonce": "nonce",
        "token_digest": "wrong",
        "status": "ISSUED",
    }

    incident = {
        "incident_id": "INC-V-002",
        "status": "AWAITING_APPROVAL",
    }

    valid, error = validate_decision_request(
        token_manager=token_mgr,
        token="wrong_token",
        claims=claims,
        contract=contract,
        incident=incident,
    )

    assert valid is False
    assert "signature" in error.lower()


def test_validate_decision_expired_token(token_mgr):
    """Test validation fails with expired token."""
    nonce = generate_approval_nonce()
    claims = build_token_claims(
        incident_id="INC-V-003",
        contract_id="CTR-V-003",
        revision=1,
        plan_hash="hash",
        nonce=nonce,
        ttl_seconds=-100,  # Expired
    )
    token = token_mgr.generate_token(claims)
    digest = ApprovalTokenManager.token_digest(token)

    contract = {
        "contract_id": "CTR-V-003",
        "incident_id": "INC-V-003",
        "revision": 1,
        "plan_hash": "hash",
        "approval_nonce": nonce,
        "token_digest": digest,
        "status": "ISSUED",
    }

    incident = {
        "incident_id": "INC-V-003",
        "status": "AWAITING_APPROVAL",
    }

    valid, error = validate_decision_request(
        token_manager=token_mgr,
        token=token,
        claims=claims,
        contract=contract,
        incident=incident,
    )

    assert valid is False
    assert "expired" in error.lower()


def test_validate_decision_wrong_contract_status(token_mgr):
    """Test validation fails when contract is not ISSUED."""
    nonce = generate_approval_nonce()
    claims = build_token_claims(
        incident_id="INC-V-004",
        contract_id="CTR-V-004",
        revision=1,
        plan_hash="hash",
        nonce=nonce,
    )
    token = token_mgr.generate_token(claims)
    digest = ApprovalTokenManager.token_digest(token)

    contract = {
        "contract_id": "CTR-V-004",
        "incident_id": "INC-V-004",
        "revision": 1,
        "plan_hash": "hash",
        "approval_nonce": nonce,
        "token_digest": digest,
        "status": "APPROVED",  # Not ISSUED
    }

    incident = {
        "incident_id": "INC-V-004",
        "status": "AWAITING_APPROVAL",
    }

    valid, error = validate_decision_request(
        token_manager=token_mgr,
        token=token,
        claims=claims,
        contract=contract,
        incident=incident,
    )

    assert valid is False
    assert "ISSUED" in error


# ── Test: Test-Mode Startup Regression ───────────────────────────────────────

def test_test_mode_empty_secret_gateway_starts(monkeypatch, tmp_path):
    """Regression: gateway must start with MUHAFIZ_TEST_MODE=true and no secret.

    Previously the throwaway secret was generated inside an except block
    that could never execute (validate() was a no-op in test mode), causing
    ApprovalTokenManager to crash on the empty string.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MUHAFIZ_TEST_MODE", "true")
    monkeypatch.setenv("MUHAFIZ_APPROVAL_SECRET", "")
    monkeypatch.setenv("MUHAFIZ_DB_PATH", str(tmp_path / "test.db"))

    from gateway.app import app

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


def test_production_mode_empty_secret_fails():
    """Production mode must refuse to start without a proper secret."""
    settings = Settings(approval_secret="", test_mode=False)
    with pytest.raises(ValueError, match="MUHAFIZ_APPROVAL_SECRET"):
        settings.validate()

