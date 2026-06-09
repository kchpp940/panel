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
            try:
                handler(session_context)
                result.executed.append(handler.name)
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
    """Register all core session cleanup handlers.

    This function is called once from :mod:`panel.io.state` to ensure
    that every built-in cleanup handler is registered regardless of
    import order and regardless of whether the corresponding resource
    module has been lazily imported.

    Handlers are idempotent — calling this function more than once is
    safe (subsequent calls are no-ops).
    """
    global _DEFAULT_HANDLERS_REGISTERED
    if _DEFAULT_HANDLERS_REGISTERED:
        return
    _DEFAULT_HANDLERS_REGISTERED = True

    # ------------------------------------------------------------------
    # priority=10 – session_info statistics
    # ------------------------------------------------------------------
    def _cleanup_session_info(session_context) -> None:
        import datetime as _dt

        from .state import state as _state

        _session_id = session_context.id
        _sessions = _state.session_info['sessions']
        if _session_id in _sessions and _sessions[_session_id]['ended'] is None:
            _session = _sessions[_session_id]
            if _session['rendered'] is not None:
                _state.session_info['live'] -= 1
            _session['ended'] = _dt.datetime.now().timestamp()
            _state.param.trigger('session_info')

    session_cleanup_registry.register(
        name="session_info",
        func=_cleanup_session_info,
        priority=10,
    )

    # ------------------------------------------------------------------
    # priority=20 – PeriodicCallback instances tracked by state._periodic
    # ------------------------------------------------------------------
    def _cleanup_periodic_callbacks(session_context) -> None:
        from .state import state as _state

        _doc = session_context._document
        if _doc in _state._periodic:
            for _cb in _state._periodic[_doc]:
                try:
                    _cb._cleanup(session_context)
                except Exception:
                    pass
            del _state._periodic[_doc]

    session_cleanup_registry.register(
        name="periodic_callbacks",
        func=_cleanup_periodic_callbacks,
        priority=20,
    )

    # ------------------------------------------------------------------
    # priority=30 – Location tracked by state._locations
    # ------------------------------------------------------------------
    def _cleanup_locations(session_context) -> None:
        from .state import state as _state

        _doc = session_context._document
        if _doc in _state._locations:
            _loc = _state._locations[_doc]
            _loc._server_destroy(session_context)
            del _state._locations[_doc]

    session_cleanup_registry.register(
        name="locations",
        func=_cleanup_locations,
        priority=30,
    )

    # ------------------------------------------------------------------
    # priority=40 – NotificationArea tracked by state._notifications
    # ------------------------------------------------------------------
    def _cleanup_notifications(session_context) -> None:
        from .state import state as _state

        _doc = session_context._document
        if _doc in _state._notifications:
            _notification = _state._notifications[_doc]
            _notification._server_destroy(session_context)
            del _state._notifications[_doc]

    session_cleanup_registry.register(
        name="notifications",
        func=_cleanup_notifications,
        priority=40,
    )

    # ------------------------------------------------------------------
    # priority=50 – BrowserInfo tracked by state._browsers
    # ------------------------------------------------------------------
    def _cleanup_browser_info(session_context) -> None:
        from .state import state as _state

        _doc = session_context._document
        if _doc in _state._browsers:
            _browser = _state._browsers[_doc]
            for _root_doc in list(_browser._documents.keys()):
                try:
                    _root = _browser._documents.get(_root_doc)
                    _browser._cleanup(_root)
                except Exception:
                    pass
            del _state._browsers[_doc]

    session_cleanup_registry.register(
        name="browser_info",
        func=_cleanup_browser_info,
        priority=50,
    )

    # ------------------------------------------------------------------
    # priority=60 – Template tracked by state._templates
    # ------------------------------------------------------------------
    def _cleanup_templates(session_context) -> None:
        from .state import state as _state

        _doc = session_context._document
        if _doc in _state._templates:
            del _state._templates[_doc]

    session_cleanup_registry.register(
        name="templates",
        func=_cleanup_templates,
        priority=60,
    )

    # ------------------------------------------------------------------
    # priority=70 – Renderable views tracked by state._views for the doc
    # ------------------------------------------------------------------
    def _cleanup_views(session_context) -> None:
        from .state import state as _state

        _doc = session_context._document
        _refs_to_remove: list[str] = []
        for _ref, (_obj, _model, _view_doc, _comm) in _state._views.items():
            if _view_doc is _doc:
                try:
                    if hasattr(_obj, '_cleanup'):
                        _obj._cleanup(_model)
                except Exception:
                    pass
                _refs_to_remove.append(_ref)
        for _ref in _refs_to_remove:
            _state._views.pop(_ref, None)

    session_cleanup_registry.register(
        name="views",
        func=_cleanup_views,
        priority=70,
    )

    # ------------------------------------------------------------------
    # priority=80 – document-scoped state dictionaries
    # ------------------------------------------------------------------
    def _cleanup_document_state(session_context) -> None:
        from .state import state as _state

        _doc = session_context._document
        _state._connected.pop(_doc, None)
        _state._loaded.pop(_doc, None)
        _state._onload.pop(_doc, None)
        _state._change_callbacks.pop(_doc, None)
        _state._stylesheets.pop(_doc, None)
        _state._extensions_.pop(_doc, None)
        _state._rel_paths.pop(_doc, None)
        _state._base_urls.pop(_doc, None)
        _state._session_outputs.pop(_doc, None)

    session_cleanup_registry.register(
        name="document_state",
        func=_cleanup_document_state,
        priority=80,
    )


__all__ = [
    "CleanupFailure",
    "CleanupFunc",
    "CleanupHandler",
    "CleanupResult",
    "SessionCleanupRegistry",
    "register_default_handlers",
    "session_cleanup_registry",
]
