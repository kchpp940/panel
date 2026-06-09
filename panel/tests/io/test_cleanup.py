"""Tests for the session cleanup handler registry (panel.io.cleanup).

Covers:
* All default handlers are registered with correct names and priorities
  when ``register_default_handlers()`` is called on a clean registry,
  regardless of prior import state.
* Every resource module exposes ``register_session_cleanup_handlers(registry)``
  and registers the handlers it owns.
* ``CleanupResult`` captures handler name and exception object for every
  failure, and subsequent handlers continue to execute after a failure.
* ``raise_if_any()`` aggregates failures into a single ``RuntimeError``.
* Registry operations (register / unregister / replace / idempotency).
"""
from __future__ import annotations

import typing as t

import pytest


_EXPECTED_DEFAULT_HANDLERS: t.List[t.Tuple[str, int]] = [
    ("session_info", 10),
    ("periodic_callbacks", 20),
    ("locations", 30),
    ("notifications", 40),
    ("browser_info", 50),
    ("templates", 60),
    ("views", 70),
    ("document_state", 80),
]

# Every module that owns session-scoped state must expose this hook,
# which is called by register_default_handlers() in cleanup.py.
_OWNER_MODULES = [
    "panel.io.callbacks",
    "panel.io.location",
    "panel.io.notifications",
    "panel.io.browser",
    "panel.io.state",
    "panel.template.base",
]


def _reset_registry() -> None:
    """Reset the global registry and registration flag for each test."""
    from panel.io import cleanup
    cleanup._DEFAULT_HANDLERS_REGISTERED = False
    from panel.io.cleanup import session_cleanup_registry
    for h in list(session_cleanup_registry.handlers):
        session_cleanup_registry.unregister(h.name)


# ---------------------------------------------------------------------------
# 1. Default-handler registration & import-order independence
# ---------------------------------------------------------------------------

class TestDefaultHandlersRegistration:
    """All default handlers are registered correctly, regardless of order."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        _reset_registry()
        yield
        _reset_registry()

    def test_all_default_handlers_registered_with_correct_priority(self):
        """register_default_handlers() registers every expected handler
        with its documented priority, in ascending priority order."""
        from panel.io.cleanup import register_default_handlers, session_cleanup_registry

        register_default_handlers()

        actual = [(h.name, h.priority) for h in session_cleanup_registry.handlers]
        assert actual == _EXPECTED_DEFAULT_HANDLERS, (
            "Default handlers mismatch (name / priority order). Got: %r" % actual
        )

    def test_register_default_handlers_is_idempotent(self):
        """Calling register_default_handlers() twice does not duplicate entries."""
        from panel.io.cleanup import register_default_handlers, session_cleanup_registry

        register_default_handlers()
        first_count = len(session_cleanup_registry.handlers)
        register_default_handlers()
        second_count = len(session_cleanup_registry.handlers)
        assert first_count == second_count == len(_EXPECTED_DEFAULT_HANDLERS)

    @pytest.mark.parametrize("modpath", _OWNER_MODULES)
    def test_every_resource_module_exposes_registration_hook(self, modpath):
        """Each module that owns session-scoped state must expose a
        ``register_session_cleanup_handlers(registry)`` function so
        that it can declare its own cleanup logic."""
        import importlib

        module = importlib.import_module(modpath)
        assert hasattr(module, "register_session_cleanup_handlers"), (
            f"{modpath} must expose register_session_cleanup_handlers(registry)"
        )
        assert callable(module.register_session_cleanup_handlers), (
            f"{modpath}.register_session_cleanup_handlers must be callable"
        )

    def test_destroy_session_triggers_registration_implicitly(self):
        """state._destroy_session() lazily calls register_default_handlers()
        so a caller that never manually triggers registration still gets
        every cleanup handler executed and a CleanupResult returned."""
        from bokeh.document import Document
        from panel.io.cleanup import CleanupResult, session_cleanup_registry
        from panel.io.state import state

        # Before _destroy_session: nothing registered yet because we
        # reset the flag at the start of this test.
        assert len(session_cleanup_registry.handlers) == 0

        doc = Document()
        ctx = type("MockCtx", (), {"_document": doc, "id": "sess-1"})()
        result = state._destroy_session(ctx)

        # Return type must be a CleanupResult.
        assert isinstance(result, CleanupResult)

        # After: every default handler must have been registered and run.
        actual_names = [h.name for h in session_cleanup_registry.handlers]
        expected_names = [n for n, _p in _EXPECTED_DEFAULT_HANDLERS]
        assert actual_names == expected_names
        assert set(result.executed) == set(expected_names)
        # No handler should have failed for an empty mock session.
        assert result.success is True
        assert result.failures == []


# ---------------------------------------------------------------------------
# 2. Failure details and continue-after-failure
# ---------------------------------------------------------------------------

class _Boom(Exception):
    pass


class TestCleanupResult:
    """CleanupResult carries the right failure info and keeps running handlers."""

    def test_failure_details_capture_name_and_exception(self):
        from panel.io.cleanup import CleanupResult, SessionCleanupRegistry

        registry = SessionCleanupRegistry()
        executed: list[str] = []

        def _first_ok(ctx):
            executed.append("first")

        def _boom(ctx):
            executed.append("boom")
            raise _Boom("kaboom")

        def _last_ok(ctx):
            executed.append("last")

        registry.register(name="first", func=_first_ok, priority=1)
        registry.register(name="boom", func=_boom, priority=2)
        registry.register(name="last", func=_last_ok, priority=3)

        result: CleanupResult = registry.run_cleanup(object())

        # All three handlers should have run even though the middle one raised.
        assert executed == ["first", "boom", "last"]
        assert result.executed == ["first", "boom", "last"]
        assert result.success is False

        # Failure detail: both the handler *name* and the exception *object*
        # must be captured.
        assert len(result.failures) == 1
        name, exc = result.failures[0]
        assert name == "boom"
        assert isinstance(exc, _Boom)
        assert str(exc) == "kaboom"

    def test_multiple_failures_all_reported(self):
        from panel.io.cleanup import SessionCleanupRegistry

        registry = SessionCleanupRegistry()

        def _err_a(ctx):
            raise ValueError("a")

        def _err_b(ctx):
            raise TypeError("b")

        registry.register(name="err_a", func=_err_a, priority=1)
        registry.register(name="err_b", func=_err_b, priority=2)

        result = registry.run_cleanup(object())

        assert result.success is False
        assert len(result.failures) == 2
        names = [name for name, _exc in result.failures]
        exc_types = [type(exc).__name__ for _name, exc in result.failures]
        assert names == ["err_a", "err_b"]
        assert exc_types == ["ValueError", "TypeError"]

    def test_subsequent_handlers_run_after_failure(self):
        """A failing handler must not prevent later handlers from running."""
        from panel.io.cleanup import SessionCleanupRegistry

        registry = SessionCleanupRegistry()
        ran: list[str] = []

        def _fails(ctx):
            ran.append("fails")
            raise RuntimeError("stop!")

        def _later(ctx):
            ran.append("later")

        registry.register(name="fails", func=_fails, priority=1)
        registry.register(name="later", func=_later, priority=100)

        result = registry.run_cleanup(object())

        assert "fails" in ran
        assert "later" in ran
        assert "later" in result.executed

    def test_raise_if_any_aggregates_messages(self):
        from panel.io.cleanup import SessionCleanupRegistry

        registry = SessionCleanupRegistry()

        def _bad(ctx):
            raise RuntimeError("oops")

        registry.register(name="bad", func=_bad, priority=1)
        result = registry.run_cleanup(object())

        with pytest.raises(RuntimeError, match=r"bad") as exc_info:
            result.raise_if_any()
        assert "oops" in str(exc_info.value)

    def test_success_result_has_no_failures(self):
        from panel.io.cleanup import SessionCleanupRegistry

        registry = SessionCleanupRegistry()
        registry.register(name="ok", func=lambda ctx: None, priority=1)
        result = registry.run_cleanup(object())

        assert result.success is True
        assert result.failures == []
        assert result.executed == ["ok"]
        # raise_if_any on a successful result is a no-op.
        result.raise_if_any()


# ---------------------------------------------------------------------------
# 3. Registry operations
# ---------------------------------------------------------------------------

class TestSessionCleanupRegistry:
    def test_register_unregister(self):
        from panel.io.cleanup import SessionCleanupRegistry

        registry = SessionCleanupRegistry()
        registry.register(name="a", func=lambda c: None, priority=5)
        registry.register(name="b", func=lambda c: None, priority=1)
        assert [h.name for h in registry.handlers] == ["b", "a"]

        registry.unregister("b")
        assert [h.name for h in registry.handlers] == ["a"]

        # Unknown name is silently ignored.
        registry.unregister("nonexistent")
        assert [h.name for h in registry.handlers] == ["a"]

    def test_register_replaces_handler_with_same_name(self):
        from panel.io.cleanup import SessionCleanupRegistry

        registry = SessionCleanupRegistry()
        calls: list[str] = []

        registry.register(name="x", func=lambda c: calls.append("old"), priority=10)
        registry.register(name="x", func=lambda c: calls.append("new"), priority=1)

        assert len(registry.handlers) == 1
        handler = registry.handlers[0]
        assert handler.priority == 1
        handler.func(object())
        assert calls == ["new"]
