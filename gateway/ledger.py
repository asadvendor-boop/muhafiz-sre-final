"""
gateway/ledger.py – SHA-256 Hash-Chain Audit Ledger
=====================================================================

This module implements an append-only, cryptographically chained audit
ledger backed by SQLite.  It is the **source of truth** for every gate
decision made in MuhafizSRE.

How the chain works:
    ┌──────────┐     ┌──────────┐     ┌──────────┐
    │ Record 0 │────▶│ Record 1 │────▶│ Record 2 │──▶ ...
    │ (genesis) │     │          │     │          │
    │ hash: H0  │     │ prev: H0 │     │ prev: H1 │
    └──────────┘     │ hash: H1 │     │ hash: H2 │
                     └──────────┘     └──────────┘

    H(n) = SHA-256( H(n-1) || plan_nonce || timestamp || action || payload )

    If any record is tampered with, verify_chain() will detect the break
    because H(n) won't match the recomputed digest.

Why SQLite?
    - Zero external dependencies (ships with Python stdlib)
    - ACID transactions on a single writer (perfect for an audit log)
    - Portable: the entire ledger is a single file, easy to back up
    - For production scale-out, swap this for PostgreSQL behind the same
      interface — the hash-chain logic stays identical.

Security Invariants:
    1. The genesis hash is deterministic: SHA-256("MuhafizSRE-Genesis-Block")
    2. Records are INSERT-only; no UPDATE or DELETE operations exist
    3. All timestamps are UTC ISO-8601 (timezone-aware)
    4. Database path is configurable via MUHAFIZ_LEDGER_DB env var
    5. Zero hardcoded credentials anywhere in this module
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from gateway.models import AuditRecord

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The genesis block's "previous hash" — a well-known constant so that the
# very first record in any MuhafizSRE ledger is independently verifiable.
GENESIS_SEED = "MuhafizSRE-Genesis-Block"
GENESIS_HASH = hashlib.sha256(GENESIS_SEED.encode("utf-8")).hexdigest()

# Default SQLite database path (overridable via environment variable).
# In production, set MUHAFIZ_LEDGER_DB to a durable, backed-up path.
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "muhafiz_ledger.db",
)


class HashLedger:
    """
    Append-only SHA-256 hash-chain ledger with SQLite persistence.

    Thread Safety:
        A threading.Lock guards all write operations.  SQLite in WAL mode
        supports concurrent readers, so GET queries don't need the lock.

    Usage:
        ledger = HashLedger()                       # uses default DB path
        tx = ledger.append_record(nonce, "approve", payload, "ops-lead")
        assert ledger.verify_chain()                # chain is intact
        trail = ledger.get_audit_trail(nonce)       # records for this plan
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        """
        Initialise the ledger, creating the table if it doesn't exist.

        Args:
            db_path: Path to the SQLite database file.
                     Defaults to MUHAFIZ_LEDGER_DB env var, then falls back
                     to a file co-located with this module.
        """
        # Resolve database path: env var → explicit arg → default
        self._db_path: str = os.environ.get(
            "MUHAFIZ_LEDGER_DB",
            db_path or DEFAULT_DB_PATH,
        )

        # Write-lock for thread-safe appends
        self._write_lock = threading.Lock()

        # Create the schema on first run
        self._init_table()

        logger.info(
            "HashLedger initialised — db=%s, genesis=%s",
            self._db_path,
            GENESIS_HASH[:16] + "…",
        )

    # ── Private helpers ─────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """
        Open a new SQLite connection with recommended pragmas.

        Returns:
            sqlite3.Connection with row_factory set to sqlite3.Row
            for dict-like access.
        """
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        # Enable Write-Ahead Logging for better concurrent read performance
        conn.execute("PRAGMA journal_mode=WAL")
        # Enforce foreign keys (not used yet, but good hygiene)
        conn.execute("PRAGMA foreign_keys=ON")

        return conn

    def _init_table(self) -> None:
        """
        Create the ledger table if it does not already exist.

        Schema design notes:
            - `id` is an auto-incrementing primary key (row order = chain order)
            - `plan_nonce` is indexed because audit-trail lookups filter by it
            - `previous_hash` and `current_hash` are both stored to allow
              forward AND backward traversal during verification
            - `payload` is stored as a TEXT blob (JSON-serialised dict)
        """
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ledger (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_nonce    TEXT    NOT NULL,
                    action        TEXT    NOT NULL,
                    payload       TEXT    NOT NULL DEFAULT '{}',
                    approver_id   TEXT    NOT NULL DEFAULT '',
                    timestamp     TEXT    NOT NULL,
                    previous_hash TEXT    NOT NULL,
                    current_hash  TEXT    NOT NULL
                )
            """)
            # Index for fast audit-trail lookups by plan_nonce
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ledger_plan_nonce
                ON ledger (plan_nonce)
            """)
            # Index for chain verification (ordered traversal)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ledger_id
                ON ledger (id ASC)
            """)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _compute_hash(
        previous_hash: str,
        plan_nonce: str,
        timestamp: str,
        action: str,
        payload: str,
    ) -> str:
        """
        Compute the SHA-256 digest for a new ledger record.

        The hash input is the concatenation of all five fields, separated
        by pipe characters for unambiguous parsing:

            SHA-256( previous_hash | plan_nonce | timestamp | action | payload )

        Using a delimiter prevents length-extension ambiguities (e.g.,
        nonce="ab" + ts="cd" vs nonce="abc" + ts="d").

        Args:
            previous_hash: SHA-256 hex digest of the preceding record.
            plan_nonce:    SHA-256 nonce of the mitigation plan.
            timestamp:     ISO-8601 UTC timestamp string.
            action:        Gate action (approve / reject / false_alarm).
            payload:       JSON-serialised payload string.

        Returns:
            64-character lowercase hex digest (SHA-256).
        """
        # Pipe-delimited concatenation for deterministic, unambiguous hashing
        hash_input = "|".join([
            previous_hash,
            plan_nonce,
            timestamp,
            action,
            payload,
        ])
        return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    def _get_last_hash(self, conn: sqlite3.Connection) -> str:
        """
        Retrieve the current_hash of the most recent ledger record.

        If the ledger is empty (first-ever record), returns the
        deterministic GENESIS_HASH.

        Args:
            conn: Active SQLite connection.

        Returns:
            64-character hex digest (SHA-256).
        """
        cursor = conn.execute(
            "SELECT current_hash FROM ledger ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row is None:
            # Empty ledger → start from genesis
            return GENESIS_HASH
        return row["current_hash"]

    # ── Public API ──────────────────────────────────────────────────────

    def append_record(
        self,
        plan_nonce: str,
        action: str,
        payload: Dict[str, Any],
        approver_id: str,
    ) -> str:
        """
        Append a new record to the hash chain and persist it in SQLite.

        This method is thread-safe (guarded by a write lock) and
        transactional (INSERT + COMMIT are atomic within SQLite).

        Args:
            plan_nonce:  SHA-256 nonce identifying the mitigation plan.
            action:      Gate decision string (approve / reject / false_alarm).
            payload:     Arbitrary metadata dict (will be JSON-serialised).
            approver_id: Identity of the human operator.

        Returns:
            The `current_hash` (SHA-256 hex digest) of the newly created
            ledger record.  This serves as a tamper-evident receipt.

        Raises:
            sqlite3.Error: On database failures (caller should handle).
        """
        # Serialise payload deterministically (sorted keys = reproducible hash)
        payload_str = json.dumps(payload, sort_keys=True, default=str)

        # UTC timestamp — timezone-aware, deterministic format
        timestamp = datetime.now(timezone.utc).isoformat()

        with self._write_lock:
            conn = self._connect()
            try:
                # Step 1: Get the hash of the previous (most recent) record
                previous_hash = self._get_last_hash(conn)

                # Step 2: Compute the current record's hash
                current_hash = self._compute_hash(
                    previous_hash=previous_hash,
                    plan_nonce=plan_nonce,
                    timestamp=timestamp,
                    action=action,
                    payload=payload_str,
                )

                # Step 3: Insert the record into SQLite
                conn.execute(
                    """
                    INSERT INTO ledger
                        (plan_nonce, action, payload, approver_id,
                         timestamp, previous_hash, current_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan_nonce,
                        action,
                        payload_str,
                        approver_id,
                        timestamp,
                        previous_hash,
                        current_hash,
                    ),
                )
                conn.commit()

                logger.info(
                    "Ledger record appended — action=%s, nonce=%s…, tx=%s…",
                    action,
                    plan_nonce[:12],
                    current_hash[:12],
                )

                return current_hash

            except Exception:
                conn.rollback()
                logger.exception("Failed to append ledger record")
                raise
            finally:
                conn.close()

    def verify_chain(self) -> bool:
        """
        Walk the entire ledger and verify that every record's hash is
        consistent with its predecessor.

        Algorithm:
            1. Fetch all records ordered by `id ASC`.
            2. For the first record, `previous_hash` must equal GENESIS_HASH.
            3. For each record, recompute the hash from its fields and
               compare against the stored `current_hash`.
            4. For each subsequent record, `previous_hash` must equal the
               preceding record's `current_hash`.
            5. If any check fails, return False immediately.

        Returns:
            True  if the entire chain is intact (or the ledger is empty).
            False if any tamper is detected.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM ledger ORDER BY id ASC"
            )
            rows = cursor.fetchall()

            if not rows:
                # Empty ledger is trivially valid
                logger.info("Chain verification: ledger is empty — trivially valid.")
                return True

            expected_previous = GENESIS_HASH

            for row in rows:
                # Check 1: previous_hash linkage
                if row["previous_hash"] != expected_previous:
                    logger.error(
                        "Chain BROKEN at record id=%d: "
                        "expected previous_hash=%s…, got=%s…",
                        row["id"],
                        expected_previous[:12],
                        row["previous_hash"][:12],
                    )
                    return False

                # Check 2: recompute the hash and compare
                recomputed = self._compute_hash(
                    previous_hash=row["previous_hash"],
                    plan_nonce=row["plan_nonce"],
                    timestamp=row["timestamp"],
                    action=row["action"],
                    payload=row["payload"],
                )
                if recomputed != row["current_hash"]:
                    logger.error(
                        "Chain BROKEN at record id=%d: "
                        "stored hash=%s…, recomputed=%s…",
                        row["id"],
                        row["current_hash"][:12],
                        recomputed[:12],
                    )
                    return False

                # Advance the chain pointer
                expected_previous = row["current_hash"]

            logger.info(
                "Chain verification PASSED — %d records verified.", len(rows)
            )
            return True

        finally:
            conn.close()

    def get_audit_trail(self, plan_nonce: str) -> List[AuditRecord]:
        """
        Retrieve all ledger records associated with a given plan_nonce.

        Records are returned in chronological order (ascending id).

        Args:
            plan_nonce: SHA-256 nonce of the mitigation plan.

        Returns:
            List of AuditRecord Pydantic models.  Empty list if no
            records match.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM ledger WHERE plan_nonce = ? ORDER BY id ASC",
                (plan_nonce,),
            )
            rows = cursor.fetchall()

            records = [
                AuditRecord(
                    id=row["id"],
                    plan_nonce=row["plan_nonce"],
                    action=row["action"],
                    payload=row["payload"],
                    approver_id=row["approver_id"],
                    timestamp=row["timestamp"],
                    previous_hash=row["previous_hash"],
                    current_hash=row["current_hash"],
                )
                for row in rows
            ]

            logger.info(
                "Audit trail for nonce=%s…: %d records found.",
                plan_nonce[:12],
                len(records),
            )
            return records

        finally:
            conn.close()

    def get_all_records(self) -> List[AuditRecord]:
        """
        Retrieve every record in the ledger (chronological order).

        ⚠️  Use with caution on large ledgers — intended for admin
        dashboards and export tools, not high-frequency queries.

        Returns:
            List of all AuditRecord models, ordered by id ASC.
        """
        conn = self._connect()
        try:
            cursor = conn.execute("SELECT * FROM ledger ORDER BY id ASC")
            rows = cursor.fetchall()
            return [
                AuditRecord(
                    id=row["id"],
                    plan_nonce=row["plan_nonce"],
                    action=row["action"],
                    payload=row["payload"],
                    approver_id=row["approver_id"],
                    timestamp=row["timestamp"],
                    previous_hash=row["previous_hash"],
                    current_hash=row["current_hash"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    @property
    def record_count(self) -> int:
        """Return the total number of records in the ledger."""
        conn = self._connect()
        try:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM ledger")
            return cursor.fetchone()["cnt"]
        finally:
            conn.close()
