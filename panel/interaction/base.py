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
    Central store for interaction events, providing event normalization,
    subscription, and filter generation.
    """

    def __init__(self) -> None:
        self._subscribers: list[Callable[[StandardEvent], None]] = []
        self._last_events: dict[str, StandardEvent] = {}

    def subscribe(self, callback: Callable[[StandardEvent], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[StandardEvent], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def publish(self, event: StandardEvent) -> None:
        self._last_events[event.kind] = event
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except Exception:
                pass

    def get_last(self, kind: str) -> StandardEvent | None:
        return self._last_events.get(kind)

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

    Subclasses only need to implement ``extract_payload`` and
    optionally ``event_names``; the shared logic for kind/source_id/
    dataset_id/selection/viewport/filters generation lives here.
    """

    _event_names: tuple[str, ...] = ()
    _store: InteractionStore | None = None

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
        self._store = store or self._default_store()

    @classmethod
    def _default_store(cls) -> InteractionStore:
        if cls._store is None:
            cls._store = InteractionStore()
        return cls._store

    @property
    def store(self) -> InteractionStore:
        return self._store

    @property
    def event_names(self) -> tuple[str, ...]:
        return self._event_names

    @abstractmethod
    def extract_payload(self, event: AdapterEvent) -> dict[str, t.Any]:
        """
        Extract component-specific payload from a raw event.

        Must return a dict that may include any of:
          - selection: dict describing the user's selection
          - viewport:  dict describing the current viewport/range
          - filters:   list of filter dicts (if already computed)
          - plus any component-specific fields under ``payload``
        """

    def get_kind(self, event: AdapterEvent) -> str:
        """
        Map the raw event name to a normalized kind.
        """
        return event.event_name

    def get_source_id(self, event: AdapterEvent) -> str:
        return self._source_id

    def get_dataset_id(self, event: AdapterEvent) -> str | None:
        return self._dataset_id

    def standardize(self, event: AdapterEvent) -> StandardEvent:
        """
        Convert a raw event into a StandardEvent using shared logic.
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

    def process_event(self, event: AdapterEvent) -> StandardEvent:
        standardized = self.standardize(event)
        self._store.publish(standardized)
        return standardized

    def register_events(
        self,
        model: Model,
        doc: Document,
        comm: Comm | None = None,
    ) -> None:
        """
        Register adapter event listeners on the underlying Bokeh model.

        The default implementation delegates to the component's
        ``_register_events`` method; subclasses may override.
        """
        if hasattr(self._component, '_register_events'):
            self._component._register_events(
                *self.event_names, model=model, doc=doc, comm=comm
            )

    def __call__(self, raw_event: t.Any, event_name: str | None = None) -> StandardEvent:
        name = event_name or getattr(raw_event, 'event_name', 'unknown')
        adapter_event = AdapterEvent(
            event_name=name, raw=raw_event, source=self._component
        )
        return self.process_event(adapter_event)
