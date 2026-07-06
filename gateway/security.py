"""
gateway/security.py – Approval Token Security for MuhafizSRE
==================================================================

HMAC-SHA256 token generation, verification, and contract security.
Implements §17: Approval token and operator identity.

Security invariants:
    - Raw tokens are NEVER persisted in events, database, or logs
    - Only SHA-256 digest of token stored in approval_contracts.token_digest
    - Use hmac.compare_digest() for ALL comparisons
    - Tokens have a 10-minute TTL with single-use semantics
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from gateway.models import canonical_json

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Settings
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Settings:
    """Application settings loaded from environment variables.

    Model Tiering (3-tier cognitive architecture):
        - speed_model:      Fast, structured tasks (Nigehban triage, Aamil execution)
        - analytical_model: Multi-step reasoning (Muhaqqiq diagnosis, Mudabbir planning)
        - safety_model:     Highest-stakes adversarial review (Muhtasib audit)
        - default_model:    Fallback for tiers not explicitly configured

    Resolution order for each tier:
        MUHAFIZ_{TIER}_MODEL  →  MUHAFIZ_DEFAULT_MODEL  →  tier default
    """

    approval_secret: str = field(default="", repr=False)
    default_model: str = "gemini-3.1-flash-lite"
    speed_model: str = "gemini-3.1-flash-lite"
    analytical_model: str = "gemini-3-flash-preview"
    safety_model: str = "gemini-3.1-pro-preview"
    victim_service_url: str = ""
    token_ttl: int = 600  # 10 minutes
    db_path: str = "data/muhafiz.db"
    test_mode: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from environment variables.

        Each tier resolves as:
            tier-specific env var → MUHAFIZ_DEFAULT_MODEL → tier default
        """
        global_fallback = os.environ.get("MUHAFIZ_DEFAULT_MODEL")
        return cls(
            approval_secret=os.environ.get("MUHAFIZ_APPROVAL_SECRET", ""),
            default_model=global_fallback or "gemini-3.1-flash-lite",
            speed_model=(
                os.environ.get("MUHAFIZ_SPEED_MODEL")
                or global_fallback
                or "gemini-3.1-flash-lite"
            ),
            analytical_model=(
                os.environ.get("MUHAFIZ_ANALYTICAL_MODEL")
                or global_fallback
                or "gemini-3-flash-preview"
            ),
            safety_model=(
                os.environ.get("MUHAFIZ_SAFETY_MODEL")
                or global_fallback
                or "gemini-3.1-pro-preview"
            ),
            victim_service_url=os.environ.get("VICTIM_SERVICE_URL", ""),
            token_ttl=int(os.environ.get("MUHAFIZ_TOKEN_TTL", "600")),
            db_path=os.environ.get("MUHAFIZ_DB_PATH", "data/muhafiz.db"),
            test_mode=os.environ.get(
                "MUHAFIZ_TEST_MODE", ""
            ).lower() in ("1", "true", "yes"),
        )


    def validate(self) -> None:
        """
        Validate settings.

        Raises ValueError if approval secret is missing or too short
        (unless test_mode is True).
        """
        if not self.test_mode:
            if not self.approval_secret or len(self.approval_secret) < 32:
                raise ValueError(
                    "MUHAFIZ_APPROVAL_SECRET must be at least 32 characters. "
                    "Set MUHAFIZ_TEST_MODE=true to bypass in development."
                )


# ────────────────────────────────────────────────────────────────────────────
# Token Manager
# ────────────────────────────────────────────────────────────────────────────

class ApprovalTokenManager:
    """
    HMAC-SHA256 approval token manager.

    Generates, verifies, and digests approval tokens using a shared
    secret. The raw token is NEVER persisted — only its SHA-256 digest.
    """

    def __init__(self, secret: str) -> None:
        """
        Initialise with a shared secret.

        Args:
            secret: HMAC key, must be at least 32 characters.
        """
        if len(secret) < 32:
            raise ValueError("Approval secret must be at least 32 characters.")
        self._secret = secret.encode("utf-8")

    def generate_token(self, claims: dict) -> str:
        """
        Generate an HMAC-SHA256 token over canonical JSON of claims.

        Args:
            claims: Token claims dict (incident_id, contract_id, etc.).

        Returns:
            Hex-encoded HMAC-SHA256 signature.
        """
        message = canonical_json(claims).encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def verify_token(self, token: str, claims: dict) -> bool:
        """
        Verify a token against claims using constant-time comparison.

        Args:
            token: The token to verify.
            claims: The claims the token should sign.

        Returns:
            True if the token is valid.
        """
        expected = self.generate_token(claims)
        return hmac.compare_digest(token, expected)

    @staticmethod
    def token_digest(token: str) -> str:
        """
        Compute SHA-256 digest of a token (for database storage).

        The raw token is NEVER stored — only this digest.

        Args:
            token: The raw HMAC token.

        Returns:
            SHA-256 hex digest of the token.
        """
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def reconstruct_token(self, claims: dict) -> str:
        """
        Reconstruct a token from claims.

        Used by the active-contract endpoint to deliver the token
        to the browser without persisting it.

        Args:
            claims: The immutable contract claims.

        Returns:
            The reconstructed HMAC-SHA256 token.
        """
        return self.generate_token(claims)


# ────────────────────────────────────────────────────────────────────────────
# Token utilities
# ────────────────────────────────────────────────────────────────────────────

def generate_approval_nonce() -> str:
    """Generate a cryptographically random 32-byte hex nonce."""
    return secrets.token_hex(32)


def build_token_claims(
    incident_id: str,
    contract_id: str,
    revision: int,
    plan_hash: str,
    nonce: str,
    ttl_seconds: int = 600,
) -> dict:
    """
    Build token claims dict with expiry.

    Args:
        incident_id: Incident identifier.
        contract_id: Contract identifier.
        revision: Contract revision number.
        plan_hash: SHA-256 hash of the canonical plan.
        nonce: Unique approval nonce.
        ttl_seconds: Token TTL in seconds (default 600 = 10 minutes).

    Returns:
        Claims dict with exp field.
    """
    return {
        "incident_id": incident_id,
        "contract_id": contract_id,
        "revision": revision,
        "plan_hash": plan_hash,
        "nonce": nonce,
        "exp": int(time.time()) + ttl_seconds,
    }


def is_token_expired(claims: dict) -> bool:
    """
    Check if token claims have expired.

    Args:
        claims: Token claims dict with 'exp' field.

    Returns:
        True if the token has expired.
    """
    exp = claims.get("exp", 0)
    return time.time() > exp


def validate_decision_request(
    token_manager: ApprovalTokenManager,
    token: str,
    claims: dict,
    contract: dict,
    incident: dict,
) -> tuple[bool, str]:
    """
    Full validation of a human decision request (§17.4).

    Validates:
        1. Token signature
        2. Expiry
        3. Token digest matches stored digest
        4. incident_id match
        5. contract_id match
        6. revision match
        7. plan_hash match
        8. nonce match
        9. Contract status is ISSUED
        10. Incident status is AWAITING_APPROVAL

    Args:
        token_manager: The ApprovalTokenManager instance.
        token: Raw HMAC token from the request.
        claims: Reconstructed claims for verification.
        contract: Contract dict from database.
        incident: Incident dict from database.

    Returns:
        (True, '') on success, (False, error_message) on failure.
    """
    # 1. Token signature
    if not token_manager.verify_token(token, claims):
        return False, "Invalid token signature."

    # 2. Expiry
    if is_token_expired(claims):
        return False, "Token has expired."

    # 3. Token digest
    computed_digest = ApprovalTokenManager.token_digest(token)
    stored_digest = contract.get("token_digest", "")
    if not hmac.compare_digest(computed_digest, stored_digest):
        return False, "Token digest mismatch."

    # 4. incident_id
    if claims.get("incident_id") != incident.get("incident_id"):
        return False, "Incident ID mismatch."

    # 5. contract_id
    if claims.get("contract_id") != contract.get("contract_id"):
        return False, "Contract ID mismatch."

    # 6. revision
    if claims.get("revision") != contract.get("revision"):
        return False, "Revision mismatch."

    # 7. plan_hash
    if claims.get("plan_hash") != contract.get("plan_hash"):
        return False, "Plan hash mismatch."

    # 8. nonce
    if claims.get("nonce") != contract.get("approval_nonce"):
        return False, "Nonce mismatch."

    # 9. Contract status
    if contract.get("status") != "ISSUED":
        return False, f"Contract is not ISSUED (current: {contract.get('status')})."

    # 10. Incident status
    if incident.get("status") != "AWAITING_APPROVAL":
        return False, (
            f"Incident is not AWAITING_APPROVAL "
            f"(current: {incident.get('status')})."
        )

    return True, ""
