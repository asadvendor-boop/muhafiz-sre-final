"""
gateway/store.py – Persistent Incident Store for MuhafizSRE
=================================================================

SQLite-backed persistence with aiosqlite, writer lock pattern, and
hash-chain event integrity. Implements §10–§11, §17.5, §20.

Invariants:
    - Process-local asyncio.Lock guards all writes
    - One fresh connection per write transaction
    - BEGIN IMMEDIATE / commit / rollback pattern
    - WAL journal mode for concurrent reads
    - Hash chain: event_hash = SHA-256(canonical envelope)

Atomic Safety Methods ():
    - claim_approval()                — single-winner approval transaction;
                                        second concurrent caller gets False
    - claim_execution_snapshot()      — APPROVED→EXECUTING with full plan
                                        revalidation (HMAC claims, plan hash,
                                        actions_json consistency); returns
                                        immutable execution snapshot
    - invalidate_tampered_contract()  — atomically INVALIDATE contract +
                                        BLOCK incident + emit plan_tampered
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import uuid
from typing import Any, Optional

import aiosqlite

from gateway.models import _utc_now, canonical_json, sha256_hex

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64  # Well-known genesis hash for empty chains
SCHEMA_VERSION = 1


class IncidentStore:
    """
    Async SQLite-backed incident store with hash-chain events.

    Usage:
        store = IncidentStore("data/muhafiz.db")
        await store.initialize()
        incident = await store.create_incident(...)
    """

    def __init__(self, db_path: str = "data/muhafiz.db") -> None:
        self._db_path = db_path
        self._writer_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Create tables and indexes if they don't exist."""
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")

            # incidents table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    alert_json TEXT NOT NULL,
                    scenario_id TEXT,
                    active_run_id TEXT,
                    active_revision INTEGER NOT NULL DEFAULT 0,
                    final_event_hash TEXT,
                    approved_by TEXT,
                    session_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # pipeline_runs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
                    phase TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    start_stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    error_type TEXT,
                    error_message TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(incident_id, phase, revision, attempt)
                )
            """)
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_pipeline_run
                ON pipeline_runs(incident_id, phase)
                WHERE status = 'RUNNING'
            """)

            # events table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(incident_id, sequence),
                    UNIQUE(incident_id, event_hash)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_incident_sequence
                ON events(incident_id, sequence)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_incident_type
                ON events(incident_id, event_type)
            """)

            # approval_contracts table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS approval_contracts (
                    contract_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    plan_event_hash TEXT NOT NULL,
                    canonical_plan_json TEXT NOT NULL,
                    actions_json TEXT NOT NULL,
                    approval_nonce TEXT NOT NULL UNIQUE,
                    token_digest TEXT NOT NULL,
                    claims_json TEXT,
                    approved_by TEXT,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_at TEXT,
                    execution_started_at TEXT,
                    consumed_at TEXT,
                    UNIQUE(incident_id, revision)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_contract_active
                ON approval_contracts(incident_id, status)
            """)
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_contract
                ON approval_contracts(incident_id)
                WHERE status IN ('ISSUED', 'APPROVED', 'EXECUTING')
            """)

            # Migration: add claims_json if missing (stale DBs)
            try:
                await db.execute(
                    "ALTER TABLE approval_contracts ADD COLUMN claims_json TEXT"
                )
            except Exception:
                pass  # Column already exists

            # room_messages table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS room_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
                    sequence INTEGER NOT NULL,
                    sender TEXT NOT NULL,
                    sender_display TEXT NOT NULL,
                    sender_emoji TEXT NOT NULL,
                    sender_color TEXT NOT NULL,
                    mentions TEXT NOT NULL DEFAULT '[]',
                    reply_to TEXT,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence_ref TEXT,
                    message_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(incident_id, sequence)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_room_messages_incident
                ON room_messages(incident_id, sequence)
            """)

            await db.commit()
        self._initialized = True
        logger.info("IncidentStore initialized: %s", self._db_path)

    # ── Read helpers ────────────────────────────────────────────────────

    async def _read_conn(self) -> aiosqlite.Connection:
        """Get a read connection with row factory."""
        db = await aiosqlite.connect(self._db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict:
        """Convert a Row to a plain dict."""
        return dict(row) if row else {}

    # ── Incident operations ─────────────────────────────────────────────

    async def create_incident(
        self,
        incident_id: str,
        alert: Any,
        scenario_id: Optional[str] = None,
    ) -> dict:
        """
        Create a new incident record.

        Args:
            incident_id: Unique incident identifier.
            alert: Alert model or dict.
            scenario_id: Optional evaluation scenario ID.

        Returns:
            The created incident as a dict.
        """
        now = _utc_now()
        alert_json = canonical_json(alert)

        # Extract alert fields
        if hasattr(alert, "model_dump"):
            alert_data = alert.model_dump()
        elif isinstance(alert, dict):
            alert_data = alert
        else:
            alert_data = {}

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute("""
                        INSERT INTO incidents
                            (incident_id, status, severity, service_id,
                             summary, alert_json, scenario_id,
                             active_revision, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        incident_id,
                        "DETECTED",
                        alert_data.get("severity", "P2"),
                        alert_data.get("service_id", "unknown"),
                        alert_data.get("summary", ""),
                        alert_json,
                        scenario_id,
                        0,
                        now,
                        now,
                    ))
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        return await self.get_incident(incident_id)  # type: ignore

    async def get_incident(self, incident_id: str) -> Optional[dict]:
        """Get an incident by ID."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM incidents WHERE incident_id = ?",
                (incident_id,),
            )
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            await db.close()

    async def list_incidents(self) -> list[dict]:
        """List all incidents ordered by creation time (newest first)."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM incidents ORDER BY created_at DESC"
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            await db.close()

    async def transition_incident(
        self,
        incident_id: str,
        from_status: str,
        to_status: str,
        **updates: Any,
    ) -> bool:
        """
        Atomic compare-and-set status transition.

        Returns True if the transition succeeded.
        """
        now = _utc_now()
        set_clauses = ["status = ?", "updated_at = ?"]
        params: list[Any] = [to_status, now]

        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            params.append(value)

        params.extend([incident_id, from_status])

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        f"UPDATE incidents SET {', '.join(set_clauses)} "
                        f"WHERE incident_id = ? AND status = ?",
                        params,
                    )
                    await db.commit()
                    return cursor.rowcount > 0
                except Exception:
                    await db.rollback()
                    raise

    async def update_incident(self, incident_id: str, **updates: Any) -> None:
        """Direct update for non-critical fields."""
        now = _utc_now()
        updates["updated_at"] = now
        set_clauses = [f"{k} = ?" for k in updates]
        params = list(updates.values()) + [incident_id]

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute(
                        f"UPDATE incidents SET {', '.join(set_clauses)} "
                        f"WHERE incident_id = ?",
                        params,
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    # ── Pipeline run operations ─────────────────────────────────────────

    async def claim_pipeline_run(
        self,
        incident_id: str,
        phase: str,
        revision: int,
        start_stage: str,
        input_data: Any,
    ) -> dict:
        """
        Atomically create or return existing RUNNING pipeline run.

        Duplicate POSTs return the existing run (idempotency).
        """
        run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"
        now = _utc_now()
        input_json = canonical_json(input_data)

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    # Check for existing running run
                    cursor = await db.execute(
                        "SELECT * FROM pipeline_runs "
                        "WHERE incident_id = ? AND phase = ? AND status = 'RUNNING'",
                        (incident_id, phase),
                    )
                    existing = await cursor.fetchone()
                    if existing:
                        await db.rollback()
                        return self._row_to_dict(existing)

                    # Determine attempt number
                    cursor = await db.execute(
                        "SELECT MAX(attempt) FROM pipeline_runs "
                        "WHERE incident_id = ? AND phase = ? AND revision = ?",
                        (incident_id, phase, revision),
                    )
                    row = await cursor.fetchone()
                    attempt = (row[0] or 0) + 1

                    await db.execute("""
                        INSERT INTO pipeline_runs
                            (run_id, incident_id, phase, revision, attempt,
                             start_stage, status, input_json, started_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        run_id, incident_id, phase, revision, attempt,
                        start_stage, "RUNNING", input_json, now,
                    ))

                    # Update incident active_run_id
                    await db.execute(
                        "UPDATE incidents SET active_run_id = ?, updated_at = ? "
                        "WHERE incident_id = ?",
                        (run_id, now, incident_id),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        return await self.get_pipeline_run(run_id)  # type: ignore

    async def get_pipeline_run(self, run_id: str) -> Optional[dict]:
        """Get a pipeline run by ID."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM pipeline_runs WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            await db.close()

    async def complete_pipeline_run(
        self, run_id: str, status: str = "COMPLETED"
    ) -> None:
        """Mark a pipeline run as completed."""
        now = _utc_now()
        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute(
                        "UPDATE pipeline_runs SET status = ?, completed_at = ? "
                        "WHERE run_id = ?",
                        (status, now, run_id),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def fail_pipeline_run(
        self, run_id: str, error_type: str, error_message: str
    ) -> None:
        """Mark a pipeline run as failed with error details."""
        now = _utc_now()
        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute(
                        "UPDATE pipeline_runs "
                        "SET status = 'FAILED', error_type = ?, "
                        "error_message = ?, completed_at = ? "
                        "WHERE run_id = ?",
                        (error_type, error_message, now, run_id),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def fail_pipeline_once(
        self,
        incident_id: str,
        run_id: str,
        phase: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """
        Idempotent pipeline failure recording.

        Only records if the run is still RUNNING or CLAIMED. Emits a
        pipeline_failed event exactly once.
        """
        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        "SELECT status FROM pipeline_runs WHERE run_id = ?",
                        (run_id,),
                    )
                    row = await cursor.fetchone()
                    if not row or row["status"] not in {"RUNNING", "CLAIMED"}:
                        await db.rollback()
                        return

                    now = _utc_now()
                    await db.execute(
                        "UPDATE pipeline_runs "
                        "SET status = 'FAILED', error_type = ?, "
                        "error_message = ?, completed_at = ? "
                        "WHERE run_id = ? AND status IN ('RUNNING', 'CLAIMED')",
                        (error_type, error_message, now, run_id),
                    )

                    # Append pipeline_failed event
                    await self._append_event_within_tx(
                        db, incident_id, run_id,
                        actor="system", actor_role="supervisor",
                        event_type="pipeline_failed",
                        summary=f"Pipeline {phase} failed: {error_type}",
                        payload={"error_type": error_type, "error_message": error_message},
                    )

                    # Transition incident to PIPELINE_FAILED
                    await db.execute(
                        "UPDATE incidents SET status = 'PIPELINE_FAILED', "
                        "updated_at = ? WHERE incident_id = ? "
                        "AND status NOT IN ('RESOLVED', 'FALSE_ALARM', 'ESCALATED')",
                        (now, incident_id),
                    )

                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def get_stale_pipeline_runs(
        self,
        statuses: tuple[str, ...] = ("RUNNING", "CLAIMED"),
        older_than_seconds: int = 600,
    ) -> list[dict]:
        """Find pipeline_runs stuck in the given statuses that are older
        than older_than_seconds. Used by PipelineSupervisor for startup
        recovery. Does NOT touch incident status — that's the supervisor's
        responsibility via fail_pipeline_once()."""
        # Use .isoformat() to match the exact format _utc_now() writes
        # to the DB (e.g. "2026-06-27T11:58:35.120050+00:00").
        # SQLite compares timestamps as strings (lexicographic), so the
        # cutoff must have identical structure — including microseconds
        # and timezone suffix — to avoid boundary comparison errors.
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=older_than_seconds)
        )
        cutoff_str = cutoff.isoformat()
        placeholders = ",".join("?" for _ in statuses)
        query = (
            f"SELECT run_id, incident_id, phase, status, started_at "
            f"FROM pipeline_runs "
            f"WHERE status IN ({placeholders}) "
            f"AND started_at < ? "
            f"ORDER BY started_at ASC"
        )
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, (*statuses, cutoff_str))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ── Event operations ────────────────────────────────────────────────

    async def _append_event_within_tx(
        self,
        db: aiosqlite.Connection,
        incident_id: str,
        run_id: str,
        actor: str,
        actor_role: str,
        event_type: str,
        summary: str,
        payload: Any = None,
    ) -> dict:
        """
        Append an event within an existing transaction.

        MUST be called while holding _writer_lock and within BEGIN IMMEDIATE.
        """
        now = _utc_now()
        payload = payload or {}
        payload_json = canonical_json(payload)

        # Get next sequence and previous hash
        cursor = await db.execute(
            "SELECT sequence, event_hash FROM events "
            "WHERE incident_id = ? ORDER BY sequence DESC LIMIT 1",
            (incident_id,),
        )
        last = await cursor.fetchone()
        sequence = (last["sequence"] + 1) if last else 1
        previous_hash = last["event_hash"] if last else GENESIS_HASH

        # Build hash envelope
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "incident_id": incident_id,
            "run_id": run_id,
            "sequence": sequence,
            "actor": actor,
            "actor_role": actor_role,
            "event_type": event_type,
            "summary": summary,
            "payload_json": payload_json,
            "previous_hash": previous_hash,
            "created_at": now,
        }
        event_hash = sha256_hex(envelope)

        await db.execute("""
            INSERT INTO events
                (incident_id, run_id, sequence, schema_version,
                 actor, actor_role, event_type, summary,
                 payload_json, previous_hash, event_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            incident_id, run_id, sequence, SCHEMA_VERSION,
            actor, actor_role, event_type, summary,
            payload_json, previous_hash, event_hash, now,
        ))

        return {
            "sequence": sequence,
            "event_hash": event_hash,
            "event_type": event_type,
            "previous_hash": previous_hash,
            "created_at": now,
        }

    async def _append_room_message_within_tx(
        self,
        db: aiosqlite.Connection,
        incident_id: str,
        sender: str,
        content: str,
        mentions: list[str] | None = None,
        reply_to: str | None = None,
        message_type: str = "analysis",
        evidence_ref: str | None = None,
    ) -> dict:
        """
        Append a room message within an existing transaction.

        MUST be called while holding _writer_lock and within BEGIN IMMEDIATE.
        """
        from gateway.room_personas import get_persona
        import json as _json

        now = _utc_now()
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        mentions = mentions or []
        persona = get_persona(sender)

        # Get next sequence and previous hash for room chain
        cursor = await db.execute(
            "SELECT sequence, message_hash FROM room_messages "
            "WHERE incident_id = ? ORDER BY sequence DESC LIMIT 1",
            (incident_id,),
        )
        last = await cursor.fetchone()
        sequence = (last["sequence"] + 1) if last else 1
        previous_hash = last["message_hash"] if last else GENESIS_HASH

        # Build hash
        hash_envelope = {
            "content": content,
            "sender": sender,
            "previous_hash": previous_hash,
            "sequence": sequence,
        }
        message_hash = sha256_hex(hash_envelope)

        await db.execute("""
            INSERT INTO room_messages
                (message_id, incident_id, sequence, sender,
                 sender_display, sender_emoji, sender_color,
                 mentions, reply_to, message_type, content,
                 evidence_ref, message_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message_id, incident_id, sequence, sender,
            persona["display_name"], persona["emoji"], persona["color"],
            _json.dumps(mentions), reply_to, message_type, content,
            evidence_ref, message_hash, now,
        ))

        return {
            "message_id": message_id,
            "incident_id": incident_id,
            "sequence": sequence,
            "sender": sender,
            "sender_display": persona["display_name"],
            "sender_emoji": persona["emoji"],
            "sender_color": persona["color"],
            "mentions": mentions,
            "reply_to": reply_to,
            "message_type": message_type,
            "content": content,
            "evidence_ref": evidence_ref,
            "message_hash": message_hash,
            "timestamp": now,
        }

    async def append_event(
        self,
        incident_id: str,
        run_id: str,
        actor: str,
        actor_role: str,
        event_type: str,
        summary: str,
        payload: Any = None,
    ) -> dict:
        """
        Append a hash-chained event to the incident timeline.

        Uses writer lock + fresh connection + BEGIN IMMEDIATE.
        """
        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    result = await self._append_event_within_tx(
                        db, incident_id, run_id,
                        actor, actor_role, event_type, summary, payload,
                    )
                    await db.commit()
                    return result
                except Exception:
                    await db.rollback()
                    raise

    async def append_event_and_room_message(
        self,
        *,
        incident_id: str,
        run_id: str,
        event_type: str,
        actor: str,
        actor_role: str,
        summary: str,
        payload: Any = None,
        room_sender: str,
        room_content: str,
        room_mentions: list[str] | None = None,
        room_reply_to: str | None = None,
        room_message_type: str = "analysis",
        transition_from: str | None = None,
        transition_to: str | None = None,
    ) -> tuple[dict, dict]:
        """
        Atomically append an event AND a room message in one transaction.

        Optionally transitions incident status within the same transaction.
        Returns (event_dict, room_message_dict).
        """
        now = _utc_now()
        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    event = await self._append_event_within_tx(
                        db, incident_id, run_id,
                        actor, actor_role, event_type, summary, payload,
                    )
                    room_msg = await self._append_room_message_within_tx(
                        db, incident_id=incident_id,
                        sender=room_sender, content=room_content,
                        mentions=room_mentions or [],
                        reply_to=room_reply_to,
                        message_type=room_message_type,
                        evidence_ref=event["event_hash"],
                    )
                    if transition_from and transition_to:
                        cursor = await db.execute(
                            "UPDATE incidents SET status = ?, updated_at = ? "
                            "WHERE incident_id = ? AND status = ?",
                            (transition_to, now, incident_id, transition_from),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError(
                                f"Status transition {transition_from} → {transition_to} "
                                f"failed for {incident_id} (matched {cursor.rowcount} rows)"
                            )
                    await db.commit()
                    return event, room_msg
                except Exception:
                    await db.rollback()
                    raise

    async def get_room_messages(
        self, incident_id: str,
    ) -> list[dict]:
        """Get all room messages for an incident."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM room_messages "
                "WHERE incident_id = ? ORDER BY sequence ASC",
                (incident_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            await db.close()

    async def get_room_messages_since(
        self, incident_id: str, after_seq: int = 0,
    ) -> list[dict]:
        """Get room messages after a given sequence number."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM room_messages "
                "WHERE incident_id = ? AND sequence > ? "
                "ORDER BY sequence ASC",
                (incident_id, after_seq),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            await db.close()

    async def get_events(
        self, incident_id: str, after: int = 0
    ) -> list[dict]:
        """Get events for an incident after a sequence number."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM events "
                "WHERE incident_id = ? AND sequence > ? "
                "ORDER BY sequence ASC",
                (incident_id, after),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            await db.close()

    async def get_latest_event_by_type(
        self, incident_id: str, event_type: str
    ) -> Optional[dict]:
        """Get the most recent event of a given type."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM events "
                "WHERE incident_id = ? AND event_type = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (incident_id, event_type),
            )
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            await db.close()

    async def get_events_by_type(
        self, incident_id: str, event_type: str
    ) -> list[dict]:
        """Get all events of a given type for an incident."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM events "
                "WHERE incident_id = ? AND event_type = ? "
                "ORDER BY sequence ASC",
                (incident_id, event_type),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            await db.close()

    async def verify_incident_chain(self, incident_id: str) -> bool:
        """Walk the event chain and verify all hashes."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM events "
                "WHERE incident_id = ? ORDER BY sequence ASC",
                (incident_id,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return True

            expected_prev = GENESIS_HASH
            for row in rows:
                row_dict = self._row_to_dict(row)

                if row_dict["previous_hash"] != expected_prev:
                    logger.error(
                        "Chain broken at seq %d: expected prev=%s, got=%s",
                        row_dict["sequence"],
                        expected_prev[:12],
                        row_dict["previous_hash"][:12],
                    )
                    return False

                # Recompute hash
                envelope = {
                    "schema_version": row_dict["schema_version"],
                    "incident_id": row_dict["incident_id"],
                    "run_id": row_dict["run_id"],
                    "sequence": row_dict["sequence"],
                    "actor": row_dict["actor"],
                    "actor_role": row_dict["actor_role"],
                    "event_type": row_dict["event_type"],
                    "summary": row_dict["summary"],
                    "payload_json": row_dict["payload_json"],
                    "previous_hash": row_dict["previous_hash"],
                    "created_at": row_dict["created_at"],
                }
                computed = sha256_hex(envelope)
                if computed != row_dict["event_hash"]:
                    logger.error(
                        "Hash mismatch at seq %d: stored=%s, computed=%s",
                        row_dict["sequence"],
                        row_dict["event_hash"][:12],
                        computed[:12],
                    )
                    return False

                expected_prev = row_dict["event_hash"]

            return True
        finally:
            await db.close()

    # ── Contract operations ─────────────────────────────────────────────

    async def issue_contract(
        self,
        incident_id: str,
        revision: int,
        plan_id: str,
        plan_hash: str,
        plan_event_hash: str,
        canonical_plan_json: str,
        actions_json: str,
        approval_nonce: str,
        token_digest: str,
        expires_at: str,
        claims_json: str = "",
    ) -> dict:
        """Issue an immutable approval contract."""
        contract_id = f"CON-{uuid.uuid4().hex[:8].upper()}"
        now = _utc_now()

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute("""
                        INSERT INTO approval_contracts
                            (contract_id, incident_id, revision, status,
                             plan_id, plan_hash, plan_event_hash,
                             canonical_plan_json, actions_json,
                             approval_nonce, token_digest, claims_json,
                             created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        contract_id, incident_id, revision, "ISSUED",
                        plan_id, plan_hash, plan_event_hash,
                        canonical_plan_json, actions_json,
                        approval_nonce, token_digest, claims_json,
                        now, expires_at,
                    ))
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        return await self.get_contract_by_id(contract_id)  # type: ignore

    async def get_active_contract(
        self, incident_id: str
    ) -> Optional[dict]:
        """Get the one active contract (ISSUED/APPROVED/EXECUTING)."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM approval_contracts "
                "WHERE incident_id = ? "
                "AND status IN ('ISSUED', 'APPROVED', 'EXECUTING') "
                "ORDER BY revision DESC LIMIT 1",
                (incident_id,),
            )
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            await db.close()

    async def get_latest_contract(
        self, incident_id: str
    ) -> Optional[dict]:
        """Get the latest contract for an incident regardless of status."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM approval_contracts "
                "WHERE incident_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (incident_id,),
            )
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            await db.close()

    async def get_contract_by_id(
        self, contract_id: str
    ) -> Optional[dict]:
        """Get a contract by its ID."""
        db = await self._read_conn()
        try:
            cursor = await db.execute(
                "SELECT * FROM approval_contracts WHERE contract_id = ?",
                (contract_id,),
            )
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            await db.close()

    async def transition_contract(
        self,
        incident_id: str,
        revision: int,
        from_status: str,
        to_status: str,
        **updates: Any,
    ) -> bool:
        """Atomic compare-and-set contract status transition."""
        set_clauses = ["status = ?"]
        params: list[Any] = [to_status]

        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            params.append(value)

        params.extend([incident_id, revision, from_status])

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        f"UPDATE approval_contracts "
                        f"SET {', '.join(set_clauses)} "
                        f"WHERE incident_id = ? AND revision = ? AND status = ?",
                        params,
                    )
                    await db.commit()
                    return cursor.rowcount > 0
                except Exception:
                    await db.rollback()
                    raise

    async def invalidate_active_contracts(
        self, incident_id: str
    ) -> int:
        """Mark all ISSUED/APPROVED contracts as INVALIDATED."""
        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await db.execute(
                        "UPDATE approval_contracts "
                        "SET status = 'INVALIDATED' "
                        "WHERE incident_id = ? "
                        "AND status IN ('ISSUED', 'APPROVED')",
                        (incident_id,),
                    )
                    await db.commit()
                    return cursor.rowcount
                except Exception:
                    await db.rollback()
                    raise

    async def claim_approval(
        self,
        incident_id: str,
        contract_id: str,
        revision: int,
        approved_by: str,
        run_id: str,
        decision_payload: dict,
    ) -> tuple[bool, str, dict]:
        """
        Atomic single-winner approval transaction (§17.5).

        Performs read-check + write in a single BEGIN IMMEDIATE transaction
        so that exactly one of N concurrent callers succeeds. The second
        concurrent caller finds the contract already APPROVED and returns
        (False, "already_claimed", {}).

        Returns:
            (True, "", event_dict)  – this caller claimed the approval
            (False, reason, {})     – contract/incident in wrong state
        """
        now = _utc_now()

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    # Re-read contract inside the transaction
                    cursor = await db.execute(
                        "SELECT status FROM approval_contracts "
                        "WHERE contract_id = ? AND incident_id = ? AND revision = ?",
                        (contract_id, incident_id, revision),
                    )
                    contract_row = await cursor.fetchone()
                    if not contract_row:
                        await db.rollback()
                        return False, "contract_not_found", {}

                    if contract_row["status"] != "ISSUED":
                        await db.rollback()
                        return (
                            False,
                            f"contract_already_claimed:{contract_row['status']}",
                            {},
                        )

                    # Re-read incident inside the transaction
                    cursor = await db.execute(
                        "SELECT status FROM incidents WHERE incident_id = ?",
                        (incident_id,),
                    )
                    incident_row = await cursor.fetchone()
                    if not incident_row:
                        await db.rollback()
                        return False, "incident_not_found", {}

                    if incident_row["status"] != "AWAITING_APPROVAL":
                        await db.rollback()
                        return (
                            False,
                            f"incident_not_awaiting:{incident_row['status']}",
                            {},
                        )

                    # ── Both checks passed – claim atomically ────────────
                    # Transition contract ISSUED → APPROVED
                    await db.execute(
                        "UPDATE approval_contracts "
                        "SET status = 'APPROVED', approved_by = ?, approved_at = ? "
                        "WHERE contract_id = ? AND status = 'ISSUED'",
                        (approved_by, now, contract_id),
                    )

                    # Transition incident AWAITING_APPROVAL → APPROVED
                    await db.execute(
                        "UPDATE incidents SET status = 'APPROVED', updated_at = ? "
                        "WHERE incident_id = ? AND status = 'AWAITING_APPROVAL'",
                        (now, incident_id),
                    )

                    # Append human_approved event to hash chain
                    event = await self._append_event_within_tx(
                        db,
                        incident_id=incident_id,
                        run_id=run_id,
                        actor=approved_by,
                        actor_role="human",
                        event_type="human_approved",
                        summary=f"Plan approved by {approved_by}",
                        payload=decision_payload,
                    )

                    await db.commit()
                    return True, "", event

                except Exception:
                    await db.rollback()
                    raise

    async def claim_execution_snapshot(
        self,
        incident_id: str,
        contract_id: str,
        revision: int,
        run_id: str,
        hmac_claims: dict,
        token_manager: "object",  # ApprovalTokenManager – avoid circular import
    ) -> tuple[bool, str, dict]:
        """
        Atomic APPROVED → EXECUTING transition with full plan revalidation (§20.1).

        Performs inside a single BEGIN IMMEDIATE:
          1. Re-fetch contract row
          2. Verify contract status == APPROVED
          3. Recompute SHA-256 of canonical_plan_json → must equal plan_hash
          4. Revalidate HMAC claims: claims["plan_hash"] == contract["plan_hash"]
          5. Verify actions_json == canonical_plan["actions"] serialised canonically
          6. APPROVED → EXECUTING transition
          7. Append plan_validated event

        Returns:
            (True, "", snapshot_dict)   – revalidation passed; snapshot is immutable
            (False, reason, {})         – tamper detected or wrong state; caller must
                                          atomically INVALIDATE/BLOCK before returning
        """
        import hmac as _hmac
        import json as _json

        now = _utc_now()

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    # 1. Re-fetch contract
                    cursor = await db.execute(
                        "SELECT * FROM approval_contracts "
                        "WHERE contract_id = ? AND incident_id = ? AND revision = ?",
                        (contract_id, incident_id, revision),
                    )
                    row = await cursor.fetchone()
                    if not row:
                        await db.rollback()
                        return False, "contract_not_found", {}

                    contract = dict(row)

                    # 2. Contract must be APPROVED (not already EXECUTING/CONSUMED)
                    if contract["status"] != "APPROVED":
                        await db.rollback()
                        return (
                            False,
                            f"contract_not_approved:{contract['status']}",
                            {},
                        )

                    # 3. Recompute canonical_plan_json → plan_hash
                    canonical_plan_json: str = contract["canonical_plan_json"]
                    canonical_plan: dict = _json.loads(canonical_plan_json)
                    recomputed_plan_hash = sha256_hex(canonical_plan)
                    stored_plan_hash: str = contract["plan_hash"]

                    if not _hmac.compare_digest(recomputed_plan_hash, stored_plan_hash):
                        await db.rollback()
                        return False, "canonical_plan_hash_mismatch", {}

                    # 4. HMAC claims["plan_hash"] must also equal stored_plan_hash
                    claims_plan_hash = hmac_claims.get("plan_hash", "")
                    if not _hmac.compare_digest(claims_plan_hash, stored_plan_hash):
                        await db.rollback()
                        return False, "claims_plan_hash_mismatch", {}

                    # 5. actions_json must exactly equal canonical_plan["actions"]
                    #    We derive actions exclusively from canonical_plan to eliminate
                    #    the duplicate actions_json column as an attack surface.
                    canonical_actions_json = canonical_json(canonical_plan.get("actions", []))
                    stored_actions_json: str = contract["actions_json"]
                    if not _hmac.compare_digest(canonical_actions_json, stored_actions_json):
                        await db.rollback()
                        return False, "actions_json_divergence", {}

                    # ── All checks passed – transition to EXECUTING ───────
                    await db.execute(
                        "UPDATE approval_contracts "
                        "SET status = 'EXECUTING', execution_started_at = ? "
                        "WHERE contract_id = ? AND status = 'APPROVED'",
                        (now, contract_id),
                    )
                    await db.execute(
                        "UPDATE incidents SET status = 'EXECUTING', updated_at = ? "
                        "WHERE incident_id = ? AND status = 'APPROVED'",
                        (now, incident_id),
                    )

                    # Append plan_validated event
                    await self._append_event_within_tx(
                        db,
                        incident_id=incident_id,
                        run_id=run_id,
                        actor="gateway",
                        actor_role="system",
                        event_type="plan_validated",
                        summary="Pre-execution plan revalidation passed",
                        payload={
                            "contract_id": contract_id,
                            "stored_plan_hash": stored_plan_hash,
                            "recomputed_plan_hash": recomputed_plan_hash,
                            "actions_json_match": True,
                        },
                    )

                    await db.commit()

                    # Return immutable execution snapshot – actions from canonical source only
                    snapshot = {
                        "contract_id": contract["contract_id"],
                        "revision": contract["revision"],
                        "plan_hash": stored_plan_hash,
                        "actions": canonical_plan.get("actions", []),
                        "canonical_plan": canonical_plan,
                    }
                    return True, "", snapshot

                except Exception:
                    await db.rollback()
                    raise

    async def invalidate_tampered_contract(
        self,
        incident_id: str,
        contract_id: str,
        run_id: str,
        reason: str,
    ) -> None:
        """
        Atomically INVALIDATE a tampered contract and BLOCK the incident (§20.2).

        Called when claim_execution_snapshot returns a tamper reason.
        """
        now = _utc_now()

        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute(
                        "UPDATE approval_contracts "
                        "SET status = 'INVALIDATED', failure_reason = ? "
                        "WHERE contract_id = ?",
                        (reason, contract_id),
                    )
                    await db.execute(
                        "UPDATE incidents SET status = 'BLOCKED', updated_at = ? "
                        "WHERE incident_id = ? AND status NOT IN "
                        "('RESOLVED', 'FALSE_ALARM', 'ESCALATED', 'BLOCKED')",
                        (now, incident_id),
                    )
                    await self._append_event_within_tx(
                        db,
                        incident_id=incident_id,
                        run_id=run_id,
                        actor="gateway",
                        actor_role="system",
                        event_type="plan_tampered",
                        summary=f"Execution aborted: pre-execution revalidation failed ({reason})",
                        payload={"reason": reason, "contract_id": contract_id},
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    # ── Transaction helpers ─────────────────────────────────────────────

    async def commit_agent_decision(
        self,
        incident_id: str,
        run_id: str,
        actor: str,
        actor_role: str,
        event_type: str,
        summary: str,
        payload: Any,
        new_incident_status: Optional[str] = None,
    ) -> dict:
        """
        Atomic: append event + optional status transition.

        Used by agent commit tools to persist decisions atomically.
        """
        now = _utc_now()
        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    event = await self._append_event_within_tx(
                        db, incident_id, run_id,
                        actor, actor_role, event_type, summary, payload,
                    )

                    if new_incident_status:
                        await db.execute(
                            "UPDATE incidents SET status = ?, updated_at = ? "
                            "WHERE incident_id = ?",
                            (new_incident_status, now, incident_id),
                        )

                    await db.commit()
                    return event
                except Exception:
                    await db.rollback()
                    raise

    async def finalize_incident(
        self,
        incident_id: str,
        run_id: str,
        contract_id: str,
        revision: int,
        final_status: str,
        reconciliation: dict,
        recovery: dict,
    ) -> dict:
        """
        Atomic finalization: outcome + contract transition + seal (§20.7).

        1. Append outcome event
        2. Transition contract
        3. Transition incident
        4. Verify chain through outcome
        5. Append seal event with pre_seal_head_hash
        6. Store final_event_hash on incident
        """
        async with self._writer_lock:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    now = _utc_now()

                    # 1. Outcome event
                    outcome_payload = {
                        "final_status": final_status,
                        "reconciliation": reconciliation,
                        "recovery": recovery,
                        "contract_id": contract_id,
                        "revision": revision,
                    }
                    outcome_event = await self._append_event_within_tx(
                        db, incident_id, run_id,
                        actor="system", actor_role="gateway",
                        event_type="outcome",
                        summary=f"Incident finalized: {final_status}",
                        payload=outcome_payload,
                    )

                    # 2. Contract transition
                    contract_to_status = (
                        "CONSUMED" if final_status == "RESOLVED" else "FAILED"
                    )
                    await db.execute(
                        "UPDATE approval_contracts SET status = ?, consumed_at = ? "
                        "WHERE contract_id = ? AND revision = ?",
                        (contract_to_status, now, contract_id, revision),
                    )

                    # 3. pre_seal_head_hash
                    pre_seal_head_hash = outcome_event["event_hash"]

                    # 4. Count events
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM events WHERE incident_id = ?",
                        (incident_id,),
                    )
                    count_row = await cursor.fetchone()
                    record_count = count_row[0] if count_row else 0

                    # 5. Seal event
                    seal_payload = {
                        "pre_seal_head_hash": pre_seal_head_hash,
                        "record_count": record_count,
                        "contract_id": contract_id,
                        "revision": revision,
                        "final_status": final_status,
                    }
                    seal_event = await self._append_event_within_tx(
                        db, incident_id, run_id,
                        actor="system", actor_role="gateway",
                        event_type="seal",
                        summary="Incident audit sealed",
                        payload=seal_payload,
                    )

                    # 6. Store final_event_hash
                    final_event_hash = seal_event["event_hash"]
                    await db.execute(
                        "UPDATE incidents "
                        "SET status = ?, final_event_hash = ?, updated_at = ? "
                        "WHERE incident_id = ?",
                        (final_status, final_event_hash, now, incident_id),
                    )

                    await db.commit()
                    return {
                        "final_status": final_status,
                        "final_event_hash": final_event_hash,
                        "pre_seal_head_hash": pre_seal_head_hash,
                        "record_count": record_count + 1,  # Including seal
                        "seal_event": seal_event,
                    }
                except Exception:
                    await db.rollback()
                    raise

    async def get_audit_proof(self, incident_id: str) -> dict:
        """
        Build audit proof for an incident (§27).

        Returns chain-valid flag, record count, final hash,
        ordered hashes, contract info, and reconciliation.
        """
        chain_valid = await self.verify_incident_chain(incident_id)
        incident = await self.get_incident(incident_id)
        events = await self.get_events(incident_id)
        contract = await self.get_latest_contract(incident_id)

        event_hashes = [e["event_hash"] for e in events]
        final_hash = incident.get("final_event_hash", "") if incident else ""

        # Get reconciliation from outcome event
        reconciliation = {}
        for e in reversed(events):
            if e.get("event_type") == "outcome":
                import json
                payload = json.loads(e.get("payload_json", "{}"))
                reconciliation = payload.get("reconciliation", {})
                break

        return {
            "chain_valid": chain_valid,
            "record_count": len(events),
            "final_event_hash": final_hash,
            "event_hashes": event_hashes,
            "contract_id": contract.get("contract_id", "") if contract else "",
            "contract_revision": contract.get("revision", 0) if contract else 0,
            "reconciliation": reconciliation,
        }
