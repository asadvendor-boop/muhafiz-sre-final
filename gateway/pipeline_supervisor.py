"""
gateway/pipeline_supervisor.py — Supervised In-Process Pipeline Runner
======================================================================

Replaces raw asyncio.create_task() in route handlers with a supervised
runner that provides:

    - Task registry with per-incident deduplication
    - Concurrency limiting via asyncio.Semaphore
    - Failure capture via store.fail_pipeline_once()
    - Conservative stale-run startup recovery
    - Graceful shutdown with task cancellation

This is NOT enterprise-grade durable orchestration. It is a supervised
in-process pipeline runner with failure capture and startup recovery.
For production deployment, replace with Temporal, Cloud Tasks, Celery,
or Step Functions (see Production Hardening Roadmap).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class PipelineSupervisor:
    """Supervised in-process pipeline runner with task tracking,
    concurrency limits, failure capture, and stale-run startup recovery."""

    def __init__(
        self,
        store: Any,
        max_concurrent: int = 2,
        stale_after_seconds: int = 600,
    ) -> None:
        self._store = store
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: dict[str, asyncio.Task] = {}  # key = "incident_id:phase"
        self._lock = asyncio.Lock()
        self._stale_after = stale_after_seconds

    # ------------------------------------------------------------------
    # Public submit methods
    # ------------------------------------------------------------------

    async def submit_phase1(
        self,
        incident_id: str,
        run_id: str,
        alert: Any,
        pipeline_fn: Callable,
    ) -> dict:
        """Submit Phase 1 pipeline (triage → diagnosis → plan → safety)."""
        key = f"{incident_id}:phase1"
        return await self._submit(
            key=key,
            phase="phase1",
            incident_id=incident_id,
            run_id=run_id,
            coro_factory=lambda: pipeline_fn(incident_id, run_id, alert),
        )

    async def submit_phase2(
        self,
        incident_id: str,
        run_id: str,
        contract: dict,
        pipeline_fn: Callable,
    ) -> dict:
        """Submit Phase 2 pipeline (execution → verification → seal)."""
        key = f"{incident_id}:phase2"
        return await self._submit(
            key=key,
            phase="phase2",
            incident_id=incident_id,
            run_id=run_id,
            coro_factory=lambda: pipeline_fn(incident_id, run_id, contract),
        )

    async def submit_revision(
        self,
        incident_id: str,
        run_id: str,
        feedback: str,
        pipeline_fn: Callable,
    ) -> dict:
        """Submit revision pipeline (re-plan → re-review)."""
        key = f"{incident_id}:revision"
        return await self._submit(
            key=key,
            phase="revision",
            incident_id=incident_id,
            run_id=run_id,
            coro_factory=lambda: pipeline_fn(incident_id, feedback),
        )

    # ------------------------------------------------------------------
    # Internal submit + supervised run
    # ------------------------------------------------------------------

    async def _submit(
        self,
        key: str,
        phase: str,
        incident_id: str,
        run_id: str,
        coro_factory: Callable[[], Coroutine],
    ) -> dict:
        """Register and launch a supervised pipeline task.

        Uses a coroutine factory (not a pre-created coroutine) to avoid
        'coroutine was never awaited' warnings on duplicate submissions.
        """
        async with self._lock:
            if key in self._active:
                logger.warning("Duplicate submit ignored: %s", key)
                return {"scheduled": False, "already_running": True, "key": key}

            task = asyncio.create_task(
                self._supervised_run(
                    key=key,
                    phase=phase,
                    incident_id=incident_id,
                    run_id=run_id,
                    coro_factory=coro_factory,
                )
            )
            self._active[key] = task

        return {"scheduled": True, "already_running": False, "key": key}

    async def _supervised_run(
        self,
        key: str,
        phase: str,
        incident_id: str,
        run_id: str,
        coro_factory: Callable[[], Coroutine],
    ) -> None:
        """Acquire semaphore, run the pipeline, capture failures, clean up."""
        async with self._semaphore:
            try:
                await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Pipeline task failed: %s", key)
                try:
                    await self._store.fail_pipeline_once(
                        incident_id=incident_id,
                        run_id=run_id,
                        phase=phase,
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:500],
                    )
                except Exception:
                    logger.exception(
                        "Failed to record pipeline failure for %s", key
                    )
            finally:
                async with self._lock:
                    self._active.pop(key, None)

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    async def recover_stale_runs(self) -> int:
        """On startup, find pipeline_runs stuck in RUNNING/CLAIMED that
        are older than stale_after_seconds and mark them PIPELINE_FAILED.

        Only touches stale pipeline_runs — does NOT touch incidents in
        AWAITING_APPROVAL, REVISION_REQUESTED, or other valid human-waiting
        states.

        Returns the number of stale runs recovered.
        """
        stale_runs = await self._store.get_stale_pipeline_runs(
            statuses=("RUNNING", "CLAIMED"),
            older_than_seconds=self._stale_after,
        )

        recovered = 0
        for run in stale_runs:
            run_id = run["run_id"]
            incident_id = run["incident_id"]
            phase = run["phase"]

            logger.warning(
                "Recovering stale pipeline run: run_id=%s, incident=%s, phase=%s",
                run_id,
                incident_id,
                phase,
            )

            try:
                await self._store.fail_pipeline_once(
                    incident_id=incident_id,
                    run_id=run_id,
                    phase=phase,
                    error_type="StaleRunRecovery",
                    error_message=(
                        f"Pipeline run was still in progress after gateway restart. "
                        f"Marked as failed by startup recovery."
                    ),
                )
                recovered += 1
            except Exception:
                logger.exception(
                    "Failed to recover stale run: %s", run_id
                )

        if recovered:
            logger.info(
                "Startup recovery: marked %d stale pipeline run(s) as PIPELINE_FAILED",
                recovered,
            )
        else:
            logger.info("Startup recovery: no stale pipeline runs found")

        return recovered

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Cancel all active tasks and await their cleanup."""
        async with self._lock:
            tasks = list(self._active.values())
            self._active.clear()

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(
                "Pipeline supervisor shutdown: cancelled %d active task(s)",
                len(tasks),
            )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_count(self) -> int:
        """Number of currently active pipeline tasks."""
        return len(self._active)
