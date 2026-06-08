"""
Defines the InteractionStore component which provides a centralized store
for cross-component interaction events (hover, selection, viewport, row selection).
"""
from __future__ import annotations

import typing as t

import param

from ..models.interaction_store import (
    InteractionEvent as _BkInteractionEvent,
    InteractionStore as _BkInteractionStore,
)
from ..reactive import Reactive
from .document import create_doc_if_none_exists, unlocked
from .state import state

if t.TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from bokeh.document import Document
    from bokeh.model import Model
    from pyviz_comms import Comm

    InteractionEventType = t.Literal["hover", "selection", "viewport", "row_selection"]


class InteractionStore(Reactive):
    """
    The InteractionStore provides a centralized store for collecting and
    distributing interaction events across multiple Panel components such
    as Plotly, Vega, ECharts, and Tabulator.

    Components publish their interaction events (hover, selection,
    viewport, row selection) to the store, and Python callbacks or other
    components can subscribe to receive events filtered by type or source.

    Reference: https://panel.holoviz.org/api/panel.io.InteractionStore.html

    :Example:

    >>> store = pn.io.InteractionStore()
    >>> plotly = pn.pane.Plotly(fig, interaction_store=store)
    >>> store.subscribe(lambda event: print(event), type="selection")
    """

    events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="""
        List of all interaction events. Each event is a dict with keys:
        - type: 'hover' | 'selection' | 'viewport' | 'row_selection'
        - source: model id of the originating component
        - data: event-specific payload
        - timestamp: monotonic timestamp in ms
        """,
    )

    hover_events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="Filtered view: events of type 'hover'",
    )

    selection_events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="Filtered view: events of type 'selection'",
    )

    viewport_events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="Filtered view: events of type 'viewport'",
    )

    row_selection_events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="Filtered view: events of type 'row_selection'",
    )

    sources = param.Dict(
        default={},
        nested_refs=True,
        doc="""
        Mapping from source model id to a human-readable source name.
        Components register themselves when publishing their first event.
        """,
    )

    max_history = param.Integer(
        default=100,
        bounds=(0, None),
        doc="Maximum number of events to keep in the history.",
    )

    _rename: t.ClassVar[Mapping[str, str | None]] = {"name": None}

    _manual_params: t.ClassVar[list[str]] = []

    def __init__(self, **params):
        super().__init__(**params)
        self._subscribers: list[tuple[
            Callable[[dict[str, t.Any]], None],
            set[str] | None,
            set[str] | None,
        ]] = []
        self._internal_callbacks.append(
            self.param.watch(self._notify_subscribers, ['events'])
        )

    def _notify_subscribers(self, *events: param.parameterized.Event) -> None:
        if not self.events:
            return
        latest = self.events[-1]
        for callback, type_filter, source_filter in self._subscribers:
            if type_filter is not None and latest['type'] not in type_filter:
                continue
            if source_filter is not None and latest['source'] not in source_filter:
                continue
            try:
                callback(latest)
            except Exception:
                import traceback
                traceback.print_exc()

    def subscribe(
        self,
        callback: Callable[[dict[str, t.Any]], None],
        type: InteractionEventType | list[InteractionEventType] | None = None,
        source: str | list[str] | None = None,
    ) -> Callable[[], None]:
        """
        Subscribe to interaction events.

        Parameters
        ----------
        callback : callable
            A function that receives a single event dict argument.
        type : str or list[str], optional
            Filter to only receive events of the given type(s).
        source : str or list[str], optional
            Filter to only receive events from the given source id(s).

        Returns
        -------
        unsubscribe : callable
            A function that when called removes the subscription.
        """
        type_set = None if type is None else (
            set(type) if isinstance(type, list) else {type}
        )
        source_set = None if source is None else (
            set(source) if isinstance(source, list) else {source}
        )
        entry = (callback, type_set, source_set)
        self._subscribers.append(entry)

        def unsubscribe() -> None:
            if entry in self._subscribers:
                self._subscribers.remove(entry)

        return unsubscribe

    def clear(
        self,
        type: InteractionEventType | None = None,
        source: str | None = None,
    ) -> None:
        """
        Clear events from the store.

        Parameters
        ----------
        type : str, optional
            If given, only clear events of this type.
        source : str, optional
            If given, only clear events from this source id.
        """
        if type is not None or source is not None:
            filtered = [
                e for e in self.events
                if (type is None or e['type'] != type)
                and (source is None or e['source'] != source)
            ]
        else:
            filtered = []
        self.events = filtered
        for ref, (model, _) in self._models.items():
            _, _, doc, comm = state._views.get(ref, (None, None, None, None))
            if comm:
                from .notebook import push
                push(doc, comm)

    def clear_all(self) -> None:
        """Clear all events from the store."""
        self.clear()

    def get_events(
        self,
        type: InteractionEventType | None = None,
        source: str | None = None,
    ) -> list[dict[str, t.Any]]:
        """
        Retrieve events filtered by type and/or source.

        Parameters
        ----------
        type : str, optional
            Only return events of this type.
        source : str, optional
            Only return events from this source id.

        Returns
        -------
        events : list[dict]
            A list of matching event dicts.
        """
        return [
            e for e in self.events
            if (type is None or e['type'] == type)
            and (source is None or e['source'] == source)
        ]

    def _process_event(self, event: _BkInteractionEvent) -> None:
        pass

    def _get_model(
        self, doc: Document, root: Model | None = None,
        parent: Model | None = None, comm: Comm | None = None
    ) -> Model:
        model = _BkInteractionStore(**self._get_properties(doc))
        root = root or model
        self._models[root.ref['id']] = (model, parent)
        self._link_props(model, self._linked_properties, doc, root, comm)
        self._register_events('interaction_event', model=model, doc=doc, comm=comm)
        return model

    def get_root(
        self, doc: Document | None = None, comm: Comm | None = None,
        preprocess: bool = True
    ) -> Model:
        doc = create_doc_if_none_exists(doc)
        root = self._get_model(doc, comm=comm)
        ref = root.ref['id']
        state._views[ref] = (self, root, doc, comm)
        self._documents[doc] = root
        return root

    def _cleanup(self, root: Model | None = None) -> None:
        if root:
            if root.document in self._documents:
                del self._documents[root.document]
            ref = root.ref['id']
        else:
            ref = None
        super()._cleanup(root)
        if ref and ref in state._views:
            del state._views[ref]
