"""Tests for the session cleanup handler registry (panel.io.cleanup).

Covers:
* All default handlers (including the autoreload no-op sentinel) are
  registered with correct names and priorities when
  ``register_default_handlers()`` is called on a clean registry.
* Every resource module (state / reload / callbacks / location /
  notifications / browser / template.base) exposes
  ``register_session_cleanup_handlers(registry)``.
* ``CleanupResult`` captures handler name and exception object for every
  failure, and subsequent handlers continue to execute after a failure.
* ``raise_if_any()`` aggregates failures into a single ``RuntimeError``.
* ``state._destroy_session()`` returns a ``CleanupResult`` whose
  ``executed`` list matches the declared priority order and whose
  ``failures`` list faithfully reflects any raised exceptions.
* Registry operations (register / unregister / replace / idempotency).
"""
from __future__ import annotations

import typing as t

import pytest


# Expected (name, priority) pairs for every default handler, in the
# ascending priority order the registry must execute them in.
_EXPECTED_DEFAULT_HANDLERS: t.List[t.Tuple[str, int]] = [
    ("session_info", 10),
    ("autoreload", 15),       # no-op sentinel — explicitly audited
    ("periodic_callbacks", 20),
    ("locations", 30),
    ("notifications", 40),
    ("browser_info", 50),
    ("templates", 60),
    ("views", 70),
    ("document_state", 80),
]

# Every module that owns (or explicitly disclaims) session-scoped state
# must expose ``register_session_cleanup_handlers(registry)``.
_OWNER_MODULES = [
    "panel.io.state",
    "panel.io.reload",        # autoreload — explicitly declares no-op
    "panel.io.callbacks",
    "panel.io.location",
    "panel.io.notifications",
    "panel.io.browser",
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
        """Each module that owns (or disclaims) session-scoped state must
        expose a ``register_session_cleanup_handlers(registry)`` function
        so that it can declare its own cleanup logic."""
        import importlib

        module = importlib.import_module(modpath)
        assert hasattr(module, "register_session_cleanup_handlers"), (
            f"{modpath} must expose register_session_cleanup_handlers(registry)"
        )
        assert callable(module.register_session_cleanup_handlers), (
            f"{modpath}.register_session_cleanup_handlers must be callable"
        )

    def test_autoreload_handler_is_registered_as_noop_sentinel(self):
        """The autoreload handler is registered even though it performs
        no work per session, so that its lifecycle is explicitly audited
        rather than being silently omitted."""
        from panel.io.cleanup import register_default_handlers, session_cleanup_registry

        register_default_handlers()

        names = [h.name for h in session_cleanup_registry.handlers]
        assert "autoreload" in names, (
            "autoreload must be an explicitly registered cleanup handler"
        )

        # Find the handler and invoke it — it must be a safe no-op that
        # raises no exception.
        autoreload_handler = next(
            h for h in session_cleanup_registry.handlers if h.name == "autoreload"
        )
        assert autoreload_handler.priority == 15
        # Should not raise for any session_context (including None / mock).
        autoreload_handler.func(None)
        autoreload_handler.func(object())

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
# 2. _destroy_session() end-to-end: execution order and failure details
# ---------------------------------------------------------------------------

class TestDestroySessionEndToEnd:
    """state._destroy_session() returns a CleanupResult with the right
    ``executed`` order and faithfully reports any failures."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        _reset_registry()
        yield
        _reset_registry()

    def test_destroy_session_executed_order_matches_priority(self):
        """The CleanupResult.executed list must follow the ascending
        priority order declared by the default handlers."""
        from bokeh.document import Document
        from panel.io.state import state

        doc = Document()
        ctx = type("MockCtx", (), {"_document": doc, "id": "sess-1"})()
        result = state._destroy_session(ctx)

        expected_order = [name for name, _prio in _EXPECTED_DEFAULT_HANDLERS]
        assert result.executed == expected_order, (
            f"Execution order mismatch. Expected {expected_order!r}, "
            f"got {result.executed!r}"
        )

    def test_destroy_session_failure_details_are_reported(self):
        """If a registered handler raises, _destroy_session() must capture
        its name and exception in CleanupResult.failures *and* continue
        running the remaining handlers."""
        from bokeh.document import Document
        from panel.io.cleanup import session_cleanup_registry
        from panel.io.state import state

        # Ensure defaults are registered first so we can inject a failure.
        from panel.io.cleanup import register_default_handlers
        register_default_handlers()

        class _Kaboom(Exception):
            pass

        ran: list[str] = []

        def _handler_before(ctx):
            ran.append("before")

        def _handler_boom(ctx):
            ran.append("boom")
            raise _Kaboom("broken")

        def _handler_after(ctx):
            ran.append("after")

        session_cleanup_registry.register(
            name="before_boom", func=_handler_before, priority=25,
        )
        session_cleanup_registry.register(
            name="boom", func=_handler_boom, priority=35,
        )
        session_cleanup_registry.register(
            name="after_boom", func=_handler_after, priority=45,
        )

        doc = Document()
        ctx = type("MockCtx", (), {"_document": doc, "id": "sess-1"})()
        result = state._destroy_session(ctx)

        # Both the before and after handler must have run — the failure
        # did not short-circuit execution.
        assert "before_boom" in result.executed
        assert "boom" in result.executed
        assert "after_boom" in result.executed
        assert ran == ["before", "boom", "after"]

        # Failure details must include the exact handler name and the
        # exact exception object (with correct type and message).
        assert result.success is False
        failure_names = [name for name, _exc in result.failures]
        assert "boom" in failure_names
        for name, exc in result.failures:
            if name == "boom":
                assert isinstance(exc, _Kaboom)
                assert str(exc) == "broken"

    def test_destroy_session_returns_cleanupresult_with_all_fields(self):
        """Sanity-check that every field of the returned CleanupResult is
        populated correctly for a successful run."""
        from bokeh.document import Document
        from panel.io.cleanup import CleanupResult
        from panel.io.state import state

        doc = Document()
        ctx = type("MockCtx", (), {"_document": doc, "id": "sess-42"})()
        result = state._destroy_session(ctx)

        assert isinstance(result, CleanupResult)
        assert isinstance(result.executed, list)
        assert isinstance(result.failures, list)
        assert isinstance(result.success, bool)
        # raise_if_any() on a successful run must be a silent no-op.
        result.raise_if_any()


# ---------------------------------------------------------------------------
# 3. CleanupResult failure semantics
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
# 4. Registry operations
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
