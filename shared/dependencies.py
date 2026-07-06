"""shared/dependencies.py — Application dependency container.

Provides module-level accessors for shared services (store, token manager,
settings) so that agent tools can access them without passing live objects
through ADK session state.

Lifecycle:
  - Each worker process (uvicorn worker, pytest worker, evaluation runner)
    calls init_dependencies() exactly once during its startup/lifespan.
  - Module-level globals are per-process; no cross-process sharing occurs.
  - Tests call reset_dependencies() in fixtures to guarantee isolation.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from gateway.store import IncidentStore
    from gateway.security import ApprovalTokenManager, Settings

_store: "IncidentStore | None" = None
_token_manager: "ApprovalTokenManager | None" = None
_settings: "Settings | None" = None


def init_dependencies(
    store: "IncidentStore",
    token_manager: "ApprovalTokenManager",
    settings: "Settings",
) -> None:
    """Called once per worker process at startup (FastAPI lifespan, test fixture, etc.)."""
    global _store, _token_manager, _settings
    _store = store
    _token_manager = token_manager
    _settings = settings


def reset_dependencies() -> None:
    """Reset all dependencies to None. Use in test teardown for isolation.

    Example (pytest fixture)::

        @pytest.fixture(autouse=True)
        def _isolated_deps(tmp_store, tmp_token_mgr, tmp_settings):
            init_dependencies(tmp_store, tmp_token_mgr, tmp_settings)
            yield
            reset_dependencies()
    """
    global _store, _token_manager, _settings
    _store = None
    _token_manager = None
    _settings = None


@contextmanager
def override_dependencies(
    store: "IncidentStore | None" = None,
    token_manager: "ApprovalTokenManager | None" = None,
    settings: "Settings | None" = None,
) -> Generator[None, None, None]:
    """Context manager to temporarily override dependencies for a test scope.

    Only overrides non-None arguments; restores originals on exit.

    Example::

        with override_dependencies(store=mock_store):
            result = await run_pipeline(...)
    """
    global _store, _token_manager, _settings
    prev_store, prev_tm, prev_settings = _store, _token_manager, _settings
    try:
        if store is not None:
            _store = store
        if token_manager is not None:
            _token_manager = token_manager
        if settings is not None:
            _settings = settings
        yield
    finally:
        _store = prev_store
        _token_manager = prev_tm
        _settings = prev_settings


def get_store() -> "IncidentStore":
    """Get the application store. Raises if not initialized."""
    if _store is None:
        raise RuntimeError(
            "Dependencies not initialized. Call init_dependencies() first."
        )
    return _store


def get_token_manager() -> "ApprovalTokenManager":
    """Get the approval token manager. Raises if not initialized."""
    if _token_manager is None:
        raise RuntimeError("Dependencies not initialized.")
    return _token_manager


def get_settings() -> "Settings":
    """Get application settings. Raises if not initialized."""
    if _settings is None:
        raise RuntimeError("Dependencies not initialized.")
    return _settings
