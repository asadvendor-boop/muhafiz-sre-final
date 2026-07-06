"""
gateway – MuhafizSRE Incident Command Room Gateway
========================================================

The gateway package provides:
    - app: FastAPI application with SSE, approval contracts, audit trail
    - models: Pydantic v2 domain models and enums
    - store: Async SQLite-backed incident store with hash-chain events
    - security: HMAC-SHA256 approval token management
    - ledger: Legacy hash-chain ledger (deprecated, kept for backward compat)
"""

__version__ = "5.0.0"
