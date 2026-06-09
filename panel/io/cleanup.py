"""
Session cleanup handler registry.

Provides a unified registry for registering and executing cleanup handlers
that run when a Bokeh/Panel session is destroyed. Each resource type
(e.g., periodic callbacks, locations, notifications, browser info)
registers its own cleanup logic, and the registry executes them in
priority order while collecting and reporting any failures.
"""
from __future__ import annotations

import logging
import typing as t

from collections.abc import Callable
from dataclasses import dataclass, field

if t.TYPE_CHECKING:
    from bokeh.application.application import SessionContext


logger = logging.getLogger(__name__)

if t.TYPE_CHECKING:
    CleanupFunc = Callable[[SessionContext], None]
else:
    CleanupFunc = Callable[..., None]

CleanupFailure = tuple[str, Exception]


@dataclass(frozen=True)
class CleanupHandler:
    """A single registered cleanup handler.

    Attributes
    ----------
    name:
        Human-readable name used for logging and failure reporting.
    priority:
        Execution priority; lower values run first.
    func:
        The callable that performs the cleanup. It receives the
        ``SessionContext`` as its only argument.
    """

    name: str
    priority: int
    func: CleanupFunc

    def __call__(self, session_context: SessionContext) -> None:
        self.func(session_context)


@dataclass
class CleanupResult:
    """Aggregated result of running all registered cleanup handlers.

    Attributes
    ----------
    executed:
        Names of handlers that were executed successfully.
    failures:
        List of ``(handler_name, exception)`` tuples for every handler
        that raised.
    """

    executed: list[str] = field(default_factory=list)
    failures: list[CleanupFailure] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.failures

    def raise_if_any(self) -> None:
        """Raise a grouped exception if any handler failed."""
        if not self.failures:
            return
        lines = [f"{name}: {type(exc).__name__}: {exc}" for name, exc in self.failures]
        raise RuntimeError(
            "One or more session cleanup handlers failed:\n  " + "\n  ".join(lines)
        )


class SessionCleanupRegistry:
    """Registry that collects and executes session cleanup handlers.

    Usage example::

        from panel.io.cleanup import session_cleanup_registry

        def cleanup_my_resource(session_context):
            ...

        session_cleanup_registry.register(
            name="my_resource",
            func=cleanup_my_resource,
            priority=50,
        )

    Handlers are executed in ascending order of ``priority`` when
    :meth:`run_cleanup` is called (typically from a single
    ``on_session_destroyed`` callback). Exceptions raised by individual
    handlers are caught and collected so that a failure in one handler
    does not prevent the others from running.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, CleanupHandler] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        func: CleanupFunc,
        priority: int = 100,
    ) -> CleanupHandler:
        """Register a cleanup handler.

        Parameters
        ----------
        name:
            Unique name for the handler. If a handler with the same name
            already exists it is replaced.
        func:
            The cleanup callable. Receives the ``SessionContext``.
        priority:
            Execution priority; lower values run first.

        Returns
        -------
        CleanupHandler
            The registered handler object.
        """
        handler = CleanupHandler(name=name, priority=priority, func=func)
        self._handlers[name] = handler
        logger.debug("Registered session cleanup handler %r (priority=%d)", name, priority)
        return handler

    def unregister(self, name: str) -> bool:
        """Remove a previously registered handler by name.

        Returns ``True`` if a handler was removed, ``False`` otherwise.
        """
        existed = name in self._handlers
        if existed:
            del self._handlers[name]
            logger.debug("Unregistered session cleanup handler %r", name)
        return existed

    def get(self, name: str) -> CleanupHandler | None:
        return self._handlers.get(name)

    @property
    def handlers(self) -> list[CleanupHandler]:
        """Return handlers sorted by priority (ascending), then name."""
        return sorted(self._handlers.values(), key=lambda h: (h.priority, h.name))

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_cleanup(self, session_context: SessionContext) -> CleanupResult:
        """Execute every registered handler in priority order.

        Parameters
        ----------
        session_context:
            The Bokeh ``SessionContext`` passed through to every handler.

        Returns
        -------
        CleanupResult
            Aggregated result listing successful handlers and any
            failures.
        """
        result = CleanupResult()
        for handler in self.handlers:
            result.executed.append(handler.name)
            try:
                handler(session_context)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Session cleanup handler %r failed", handler.name
                )
                result.failures.append((handler.name, exc))
        return result


# ---------------------------------------------------------------------------
# Global registry instance
# ---------------------------------------------------------------------------

session_cleanup_registry = SessionCleanupRegistry()


# ---------------------------------------------------------------------------
# Default handler registration
# ---------------------------------------------------------------------------

_DEFAULT_HANDLERS_REGISTERED = False


def register_default_handlers() -> None:
    """Register all built-in session cleanup handlers.

    This function explicitly imports every resource module that owns
    session-scoped state and delegates handler registration to each
    module via its :func:`register_session_cleanup_handlers` hook.

    This approach ensures that:

    * Handlers are always registered regardless of whether the resource
      module was lazily imported elsewhere (no import-order dependency).
    * Every resource owns and declares its own cleanup logic — this
      function contains **no** resource-specific cleanup details.
    * Calling this function more than once is a no-op.
    """
    import sys as _sys
    import importlib as _importlib

    global _DEFAULT_HANDLERS_REGISTERED
    if _DEFAULT_HANDLERS_REGISTERED:
        return
    _DEFAULT_HANDLERS_REGISTERED = True

    # --- Fully-qualified module names of every resource module that ---
    # --- owns session-scoped state. Order here does not matter because
    # --- handlers are sorted by priority at execution time.
    _MODULE_PATHS: t.List[str] = [
        "panel.io.state",
        "panel.io.callbacks",
        "panel.io.location",
        "panel.io.notifications",
        "panel.io.browser",
        "panel.template.base",
    ]

    # NOTE: we must import modules cleanly via ``importlib.import_module``
    # and then look them up in sys.modules, because ``panel.io.__init__``
    # re-exports the *singleton instances* ``state`` and ``cache`` under
    # the package namespace — doing ``from . import state`` would give us
    # the _state() *object* instead of the panel.io.state *module*.
    for _path in _MODULE_PATHS:
        _importlib.import_module(_path)
        _module = _sys.modules[_path]
        _module.register_session_cleanup_handlers(session_cleanup_registry)


__all__ = [
    "CleanupFailure",
    "CleanupFunc",
    "CleanupHandler",
    "CleanupResult",
    "SessionCleanupRegistry",
    "register_default_handlers",
    "session_cleanup_registry",
]
