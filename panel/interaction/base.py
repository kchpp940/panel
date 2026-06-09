from __future__ import annotations

import typing as t
import uuid

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

if t.TYPE_CHECKING:
    from collections.abc import Callable
    from bokeh.document import Document
    from bokeh.model import Model
    from pyviz_comms import Comm


@dataclass
class StandardEvent:
    """
    Standardized interaction event containing normalized fields
    common across all component types.

    Fields:
      - kind: normalized event type (e.g. "point_click", "selection",
        "viewport_change")
      - source_id: unique identifier of the originating component
      - dataset_id: optional identifier of the underlying dataset
      - selection: normalized selection dict (indexes, field, min/max,
        values, ...)
      - viewport: normalized viewport dict (axis ranges, pagination,
        sorters, ...)
      - filters: list of filter specs, auto-generated from ``selection``
        when possible
      - payload: any component-specific fields extracted from the raw
        event that do not fall into the categories above
      - event_id: auto-generated unique event id
      - timestamp: auto-generated POSIX timestamp
    """

    kind: str
    source_id: str
    dataset_id: str | None = None
    selection: dict[str, t.Any] | None = None
    viewport: dict[str, t.Any] | None = None
    filters: list[dict[str, t.Any]] | None = None
    payload: dict[str, t.Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=lambda: __import__('time').time())


@dataclass
class AdapterEvent:
    """
    Wrapper for a raw component event before standardization.
    """

    event_name: str
    raw: t.Any
    source: t.Any


class InteractionStore:
    """
    Per-component event store.

    Each :class:`InteractionAdapter` owns its own ``InteractionStore``
    instance — stores are never shared across components or sessions.

    Provides:
      * ``subscribe`` / ``unsubscribe`` with optional ``source_id`` or
        ``kind`` filtering
      * ``publish`` — delivers an event to all matching subscribers
      * ``get_last`` / ``get_last_by_source`` — latest-event lookup
      * ``generate_filters`` — shared static helper for deriving filter
        specs from a normalized selection
    """

    def __init__(self) -> None:
        self._subscribers: list[Callable[[StandardEvent], None]] = []
        self._source_subscribers: dict[str, list[Callable[[StandardEvent], None]]] = {}
        self._kind_subscribers: dict[str, list[Callable[[StandardEvent], None]]] = {}
        self._last_events: dict[str, StandardEvent] = {}
        self._last_by_source: dict[str, StandardEvent] = {}

    def subscribe(
        self,
        callback: Callable[[StandardEvent], None],
        *,
        source_id: str | None = None,
        kind: str | None = None,
    ) -> None:
        if source_id is not None:
            self._source_subscribers.setdefault(source_id, []).append(callback)
            return
        if kind is not None:
            self._kind_subscribers.setdefault(kind, []).append(callback)
            return
        self._subscribers.append(callback)

    def unsubscribe(
        self,
        callback: Callable[[StandardEvent], None],
        *,
        source_id: str | None = None,
        kind: str | None = None,
    ) -> None:
        if source_id is not None:
            lst = self._source_subscribers.get(source_id, [])
            if callback in lst:
                lst.remove(callback)
            return
        if kind is not None:
            lst = self._kind_subscribers.get(kind, [])
            if callback in lst:
                lst.remove(callback)
            return
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def publish(self, event: StandardEvent) -> None:
        self._last_events[event.kind] = event
        self._last_by_source[event.source_id] = event
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception:
                pass
        for subscriber in list(self._source_subscribers.get(event.source_id, [])):
            try:
                subscriber(event)
            except Exception:
                pass
        for subscriber in list(self._kind_subscribers.get(event.kind, [])):
            try:
                subscriber(event)
            except Exception:
                pass

    def get_last(self, kind: str) -> StandardEvent | None:
        return self._last_events.get(kind)

    def get_last_by_source(self, source_id: str) -> StandardEvent | None:
        return self._last_by_source.get(source_id)

    @staticmethod
    def generate_filters(selection: dict[str, t.Any] | None) -> list[dict[str, t.Any]]:
        """
        Derive filter specifications from a normalized selection.

        Supported selection shapes:

        * Point indexes  — ``{'indexes': [...], 'field': '...'}``
        * Range bounds   — ``{'field': 'x', 'min': ..., 'max': ...}``
        * Categorical    — ``{'field': 'category', 'values': [...]}``
        """
        if not selection:
            return []
        filters: list[dict[str, t.Any]] = []
        if 'indexes' in selection and selection['indexes']:
            filters.append({
                'type': 'in',
                'field': selection.get('field', 'index'),
                'value': list(selection['indexes']),
            })
        if 'field' in selection and ('min' in selection or 'max' in selection):
            f = {'field': selection['field'], 'type': 'range'}
            if 'min' in selection:
                f['min'] = selection['min']
            if 'max' in selection:
                f['max'] = selection['max']
            filters.append(f)
        if 'values' in selection and selection['values']:
            filters.append({
                'type': 'in',
                'field': selection.get('field', 'value'),
                'value': list(selection['values']),
            })
        return filters


class InteractionAdapter(ABC):
    """
    Abstract base class for component interaction adapters.

    **Scope of responsibility (kept intentionally narrow):**
      1. ``extract_payload`` — pull selection / viewport / extra fields
         out of the raw component event (the *only* subclass-specific
         step).
      2. Field mapping — ``get_kind`` / ``get_source_id`` /
         ``get_dataset_id`` with sensible defaults that subclasses may
         override.
      3. ``standardize`` — assemble a :class:`StandardEvent` by gluing
         together the pieces above, auto-generating ``filters`` from the
         selection via :meth:`InteractionStore.generate_filters`.
      4. ``handle_event`` — wrap the raw event, standardize it, and
         publish to the adapter's own :class:`InteractionStore`.

    Adapters do **not** contain component business logic. Side effects
    such as updating component parameters or invoking user callbacks
    live in the component itself; components simply call
    ``adapter.handle_event(raw_event)`` from their ``_process_event``
    alongside their own logic.
    """

    _event_names: tuple[str, ...] = ()

    def __init__(
        self,
        component: t.Any,
        source_id: str | None = None,
        dataset_id: str | None = None,
        store: InteractionStore | None = None,
    ) -> None:
        self._component = component
        self._source_id = (
            source_id
            or getattr(component, 'name', None)
            or f'{type(component).__name__}-{id(component)}'
        )
        self._dataset_id = dataset_id
        self._store = store if store is not None else InteractionStore()

    @property
    def store(self) -> InteractionStore:
        """The per-adapter event store (never shared across components)."""
        return self._store

    @property
    def component(self) -> t.Any:
        return self._component

    @property
    def event_names(self) -> tuple[str, ...]:
        return self._event_names

    @abstractmethod
    def extract_payload(self, event: AdapterEvent) -> dict[str, t.Any]:
        """
        Extract selection, viewport, and any component-specific fields
        from ``event.raw``.

        Return shape (all keys optional):

        * ``selection`` — normalized selection dict
        * ``viewport``  — normalized viewport / pagination / sorters dict
        * ``filters``   — pre-computed filter list (if absent,
          :meth:`InteractionStore.generate_filters` derives it from
          ``selection``)
        * any extra keys are forwarded to ``StandardEvent.payload``
        """

    def get_kind(self, event: AdapterEvent) -> str:
        """Map the raw event name to a normalized ``kind`` string."""
        return event.event_name

    def get_source_id(self, event: AdapterEvent) -> str:
        return self._source_id

    def get_dataset_id(self, event: AdapterEvent) -> str | None:
        return self._dataset_id

    def standardize(self, event: AdapterEvent) -> StandardEvent:
        """
        Convert a raw :class:`AdapterEvent` into a :class:`StandardEvent`.

        Shared logic for ``kind`` / ``source_id`` / ``dataset_id`` /
        ``selection`` / ``viewport`` / ``filters`` lives here and is
        identical across all adapters.
        """
        extracted = self.extract_payload(event)
        selection = extracted.pop('selection', None)
        viewport = extracted.pop('viewport', None)
        filters = extracted.pop('filters', None)

        if filters is None and selection:
            filters = self._store.generate_filters(selection)

        return StandardEvent(
            kind=self.get_kind(event),
            source_id=self.get_source_id(event),
            dataset_id=self.get_dataset_id(event),
            selection=selection,
            viewport=viewport,
            filters=filters,
            payload=extracted,
        )

    def handle_event(
        self,
        raw_event: t.Any,
        event_name: str | None = None,
    ) -> StandardEvent:
        """
        Full pipeline: wrap → standardize → publish to this adapter's
        :class:`InteractionStore`.

        Components call this from their ``_process_event`` handlers,
        **in addition** to running their own side-effect logic.
        """
        name = event_name or getattr(raw_event, 'event_name', 'unknown')
        adapter_event = AdapterEvent(
            event_name=name, raw=raw_event, source=self._component
        )
        standardized = self.standardize(adapter_event)
        self._store.publish(standardized)
        return standardized

    def __call__(
        self,
        raw_event: t.Any,
        event_name: str | None = None,
    ) -> StandardEvent:
        return self.handle_event(raw_event, event_name)

    def register_events(
        self,
        model: Model,
        doc: Document,
        comm: Comm | None = None,
    ) -> None:
        if hasattr(self._component, '_register_events'):
            self._component._register_events(
                *self.event_names, model=model, doc=doc, comm=comm
            )
