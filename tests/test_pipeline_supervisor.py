"""
tests/test_pipeline_supervisor.py — PipelineSupervisor Unit Tests
==================================================================

9 tests covering:
1. No raw asyncio.create_task in route handlers
2. submit_phase1 schedules one task
3. Duplicate submit is deduped
4. Duplicate submit does not leak unawaited coroutine
5. Uncaught task exception becomes PIPELINE_FAILED
6. Semaphore limits concurrency
7. Stale RUNNING/CLAIMED pipeline runs are recovered on startup
8. AWAITING_APPROVAL is not marked stale
9. Shutdown cancels active tasks and clears registry
"""

from __future__ import annotations

import ast
import asyncio
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.pipeline_supervisor import PipelineSupervisor


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_mock_store(stale_runs=None):
    """Create a mock store with the methods PipelineSupervisor needs."""
    store = AsyncMock()
    store.fail_pipeline_once = AsyncMock()
    store.get_stale_pipeline_runs = AsyncMock(return_value=stale_runs or [])
    return store


async def _noop_pipeline(*args, **kwargs):
    """A pipeline function that completes immediately."""
    await asyncio.sleep(0.01)


async def _slow_pipeline(*args, **kwargs):
    """A pipeline function that takes a while."""
    await asyncio.sleep(0.5)


async def _failing_pipeline(*args, **kwargs):
    """A pipeline function that raises."""
    raise RuntimeError("Simulated pipeline crash")


# ── Test 1: No raw create_task in route handlers ────────────────────────


class TestNoRawCreateTaskInRouteHandlers:
    """Verify that gateway/app.py route handlers do not contain
    asyncio.create_task calls — they should use the supervisor."""

    def test_no_raw_create_task_in_route_handlers(self):
        """AST-parse gateway/app.py and inspect create_incident() and
        submit_decision() for asyncio.create_task calls."""
        app_path = Path(__file__).parent.parent / "gateway" / "app.py"
        source = app_path.read_text()
        tree = ast.parse(source)

        # Find the route handler functions
        target_functions = {"create_incident", "submit_decision"}
        violations = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.name in target_functions:
                    # Walk the function body for asyncio.create_task calls
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            func = child.func
                            # Match asyncio.create_task(...)
                            if (
                                isinstance(func, ast.Attribute)
                                and func.attr == "create_task"
                                and isinstance(func.value, ast.Name)
                                and func.value.id == "asyncio"
                            ):
                                violations.append(
                                    f"{node.name}() at line {child.lineno}"
                                )

        assert not violations, (
            f"Raw asyncio.create_task found in route handlers: {violations}. "
            f"Use app.state.pipeline_supervisor.submit_*() instead."
        )


# ── Test 2: submit_phase1 schedules one task ────────────────────────────


@pytest.mark.asyncio
async def test_submit_phase1_schedules_task():
    store = _make_mock_store()
    supervisor = PipelineSupervisor(store=store, max_concurrent=2)

    result = await supervisor.submit_phase1(
        incident_id="inc-1",
        run_id="run-1",
        alert={"severity": "P1"},
        pipeline_fn=_noop_pipeline,
    )

    assert result["scheduled"] is True
    assert result["already_running"] is False
    assert result["key"] == "inc-1:phase1"
    assert supervisor.active_count >= 0  # Task may have finished already

    # Wait for task to complete
    await asyncio.sleep(0.1)
    assert supervisor.active_count == 0


# ── Test 3: Duplicate submit is deduped ─────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_submit_deduped():
    store = _make_mock_store()
    supervisor = PipelineSupervisor(store=store, max_concurrent=2)

    # First submit — should schedule
    result1 = await supervisor.submit_phase1(
        incident_id="inc-1",
        run_id="run-1",
        alert={"severity": "P1"},
        pipeline_fn=_slow_pipeline,
    )
    assert result1["scheduled"] is True

    # Second submit with same incident — should be deduped
    result2 = await supervisor.submit_phase1(
        incident_id="inc-1",
        run_id="run-1",
        alert={"severity": "P1"},
        pipeline_fn=_slow_pipeline,
    )
    assert result2["scheduled"] is False
    assert result2["already_running"] is True

    await supervisor.shutdown()


# ── Test 4: Duplicate submit does not leak unawaited coroutine ──────────


@pytest.mark.asyncio
async def test_duplicate_submit_no_unawaited_coroutine():
    """Duplicate submit must not create a coroutine object that is never
    awaited (which would produce RuntimeWarning)."""
    store = _make_mock_store()
    supervisor = PipelineSupervisor(store=store, max_concurrent=2)

    # First submit
    await supervisor.submit_phase1(
        incident_id="inc-1",
        run_id="run-1",
        alert={"severity": "P1"},
        pipeline_fn=_slow_pipeline,
    )

    # Second submit — should NOT produce RuntimeWarning
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        await supervisor.submit_phase1(
            incident_id="inc-1",
            run_id="run-1",
            alert={"severity": "P1"},
            pipeline_fn=_slow_pipeline,
        )

    coroutine_warnings = [
        x for x in w if "coroutine" in str(x.message).lower()
    ]
    assert not coroutine_warnings, (
        f"Duplicate submit leaked unawaited coroutine: {coroutine_warnings}"
    )

    await supervisor.shutdown()


# ── Test 5: Task exception becomes PIPELINE_FAILED ──────────────────────


@pytest.mark.asyncio
async def test_task_exception_becomes_pipeline_failed():
    store = _make_mock_store()
    supervisor = PipelineSupervisor(store=store, max_concurrent=2)

    await supervisor.submit_phase1(
        incident_id="inc-crash",
        run_id="run-crash",
        alert={"severity": "P1"},
        pipeline_fn=_failing_pipeline,
    )

    # Wait for task to complete and exception to be handled
    await asyncio.sleep(0.2)

    store.fail_pipeline_once.assert_called_once_with(
        incident_id="inc-crash",
        run_id="run-crash",
        phase="phase1",
        error_type="RuntimeError",
        error_message="Simulated pipeline crash",
    )
    assert supervisor.active_count == 0


# ── Test 6: Semaphore limits concurrency ────────────────────────────────


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency():
    store = _make_mock_store()
    supervisor = PipelineSupervisor(store=store, max_concurrent=1)

    execution_order = []

    async def pipeline_a(*args):
        execution_order.append("a_start")
        await asyncio.sleep(0.2)
        execution_order.append("a_end")

    async def pipeline_b(*args):
        execution_order.append("b_start")
        await asyncio.sleep(0.1)
        execution_order.append("b_end")

    await supervisor.submit_phase1(
        incident_id="inc-a",
        run_id="run-a",
        alert={},
        pipeline_fn=pipeline_a,
    )
    await supervisor.submit_phase1(
        incident_id="inc-b",
        run_id="run-b",
        alert={},
        pipeline_fn=pipeline_b,
    )

    # Wait for both to complete
    await asyncio.sleep(0.8)

    # With max_concurrent=1, b should not start until a finishes
    assert execution_order.index("a_end") < execution_order.index("b_start"), (
        f"Semaphore did not limit concurrency: {execution_order}"
    )


# ── Test 7: Startup recovery marks stale RUNNING runs ───────────────────


@pytest.mark.asyncio
async def test_startup_recovery_marks_stale_runs():
    stale_runs = [
        {
            "run_id": "run-stale-1",
            "incident_id": "inc-stale-1",
            "phase": "phase1",
            "status": "RUNNING",
            "started_at": "2020-01-01T00:00:00",
        },
        {
            "run_id": "run-stale-2",
            "incident_id": "inc-stale-2",
            "phase": "phase2",
            "status": "CLAIMED",
            "started_at": "2020-01-01T00:00:00",
        },
    ]
    store = _make_mock_store(stale_runs=stale_runs)
    supervisor = PipelineSupervisor(
        store=store, max_concurrent=2, stale_after_seconds=600
    )

    recovered = await supervisor.recover_stale_runs()

    assert recovered == 2
    assert store.fail_pipeline_once.call_count == 2

    # Verify first call
    call_args = store.fail_pipeline_once.call_args_list[0]
    assert call_args.kwargs["incident_id"] == "inc-stale-1"
    assert call_args.kwargs["run_id"] == "run-stale-1"
    assert call_args.kwargs["error_type"] == "StaleRunRecovery"


# ── Test 8: AWAITING_APPROVAL is not marked stale ───────────────────────


@pytest.mark.asyncio
async def test_awaiting_approval_not_marked_stale():
    """Startup recovery only touches pipeline_runs in RUNNING/CLAIMED.
    Incidents in AWAITING_APPROVAL are not affected because their
    pipeline_runs have already completed (status != RUNNING)."""
    store = _make_mock_store(stale_runs=[])  # No stale runs returned
    supervisor = PipelineSupervisor(
        store=store, max_concurrent=2, stale_after_seconds=600
    )

    recovered = await supervisor.recover_stale_runs()

    assert recovered == 0
    store.fail_pipeline_once.assert_not_called()

    # Verify the query was called with correct statuses
    store.get_stale_pipeline_runs.assert_called_once_with(
        statuses=("RUNNING", "CLAIMED"),
        older_than_seconds=600,
    )


# ── Test 9: Shutdown cancels active tasks and clears registry ───────────


@pytest.mark.asyncio
async def test_shutdown_cancels_active_tasks():
    store = _make_mock_store()
    supervisor = PipelineSupervisor(store=store, max_concurrent=2)

    # Start a slow task
    await supervisor.submit_phase1(
        incident_id="inc-shutdown",
        run_id="run-shutdown",
        alert={},
        pipeline_fn=_slow_pipeline,
    )
    assert supervisor.active_count == 1

    # Shutdown
    await supervisor.shutdown()

    assert supervisor.active_count == 0
