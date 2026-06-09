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
    Wrapper for raw component events before standardization.
    """

    event_name: str
    raw: t.Any
    source: t.Any


class InteractionStore:
    """
    Central singleton store for interaction events.

    Provides global publish/subscribe, last-event lookup, and shared
    filter generation logic. Use ``InteractionStore.instance()`` to
    obtain the process-wide singleton.
    """

    _instance: InteractionStore | None = None

    def __init__(self) -> None:
        self._subscribers: list[Callable[[StandardEvent], None]] = []
        self._source_subscribers: dict[str, list[Callable[[StandardEvent], None]]] = {}
        self._kind_subscribers: dict[str, list[Callable[[StandardEvent], None]]] = {}
        self._last_events: dict[str, StandardEvent] = {}
        self._last_by_source: dict[str, StandardEvent] = {}

    @classmethod
    def instance(cls) -> InteractionStore:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

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
        Generate filter specifications from a selection.

        The selection dict may contain:
          - point indexes: {'indexes': [...], 'field': '...'}
          - range bounds:  {'field': 'x', 'min': ..., 'max': ...}
          - categorical:  {'field': 'category', 'values': [...]}
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

    **Event flow contract:**
      1. Component's ``_process_event`` calls ``adapter.handle_event(raw_event)``.
      2. ``handle_event`` wraps the raw event in an ``AdapterEvent``.
      3. ``standardize`` runs: ``extract_payload`` (subclass) -> fill
         ``kind``/``source_id``/``dataset_id``/``selection``/``viewport``/``filters``.
      4. ``on_standardized_event`` (subclass hook) is called with the
         ``StandardEvent`` — this is where the component's legacy logic
         should live (e.g. updating ``self.selection``, invoking user
         callbacks, FigureWidget point handlers).
      5. The event is published to the global ``InteractionStore``.

    Subclasses only implement:
      * ``extract_payload`` — pull selection / viewport / component-specific
        fields out of the raw event.
      * ``on_standardized_event`` (optional) — any per-component side
        effects that must run after standardization.
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
        self._source_id = source_id or getattr(component, 'name', None) or f'{type(component).__name__}-{id(component)}'
        self._dataset_id = dataset_id
        self._store = store or InteractionStore.instance()

    @property
    def store(self) -> InteractionStore:
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
        Extract component-specific payload from a raw event.

        Return a dict that may contain:
          * ``selection`` — normalized selection dict
          * ``viewport`` — viewport / range dict
          * ``filters`` — pre-computed filter list (optional, otherwise
            ``InteractionStore.generate_filters`` is used)
          * any additional component-specific fields — these end up in
            ``StandardEvent.payload``
        """

    def get_kind(self, event: AdapterEvent) -> str:
        return event.event_name

    def get_source_id(self, event: AdapterEvent) -> str:
        return self._source_id

    def get_dataset_id(self, event: AdapterEvent) -> str | None:
        return self._dataset_id

    def standardize(self, event: AdapterEvent) -> StandardEvent:
        """
        Convert a raw event into a ``StandardEvent``.

        Shared logic for ``kind``/``source_id``/``dataset_id``/
        ``selection``/``viewport``/``filters`` lives here; subclasses
        only contribute via ``extract_payload`` and the optional
        mapping hooks.
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

    def on_standardized_event(self, event: StandardEvent, raw: AdapterEvent) -> None:
        """
        Hook invoked after standardization but *before* publishing.

        Override in subclasses to execute component-specific legacy
        logic (e.g. updating component parameters, invoking user
        callbacks). The default implementation is a no-op.
        """

    def handle_event(
        self,
        raw_event: t.Any,
        event_name: str | None = None,
    ) -> StandardEvent:
        """
        Main entry point — the component's ``_process_event`` should
        delegate to this method.

        Flow: wrap → standardize → ``on_standardized_event`` hook →
        publish to ``InteractionStore``.
        """
        name = event_name or getattr(raw_event, 'event_name', 'unknown')
        adapter_event = AdapterEvent(
            event_name=name, raw=raw_event, source=self._component
        )
        standardized = self.standardize(adapter_event)
        self.on_standardized_event(standardized, adapter_event)
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
