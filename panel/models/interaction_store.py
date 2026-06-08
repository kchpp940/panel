"""
Defines the InteractionStore model which provides a centralized store
for cross-component interaction events (hover, selection, viewport, row selection).
"""
from __future__ import annotations

import typing as t

from bokeh.core.enums import enumeration
from bokeh.core.properties import (
    Any, Bool, Dict, Int, List, String,
)
from bokeh.events import ModelEvent
from bokeh.models import Model

from ..config import config
from ..io.resources import bundled_files
from ..util import classproperty

InteractionEventKind = t.Literal["hover", "selection", "viewport", "row_selection"]

InteractionEventKinds = enumeration("hover", "selection", "viewport", "row_selection")


class InteractionEvent(ModelEvent):

    event_name = 'interaction_event'

    def __init__(
        self,
        model,
        kind: InteractionEventKind | None = None,
        source: str | None = None,
        source_id: str | None = None,
        payload: t.Any = None,
        selection: t.Any = None,
        viewport: t.Any = None,
    ):
        self.kind = kind
        self.source = source
        self.source_id = source_id
        self.payload = payload
        self.selection = selection
        self.viewport = viewport
        super().__init__(model=model)

    def __repr__(self):
        return (
            f'{type(self).__name__}(kind={self.kind!r}, source={self.source!r}, '
            f'source_id={self.source_id!r}, selection={self.selection!r}, '
            f'viewport={self.viewport!r})'
        )


class InteractionStore(Model):
    """
    A centralized store for collecting and distributing interaction events
    across multiple Panel components (Plotly, Vega, ECharts, Tabulator, etc.).

    Components publish their events to the store, and other components or
    Python callbacks can subscribe to receive filtered events by kind
    or source.

    Each event has a normalized schema:
        - kind: "hover" | "selection" | "viewport" | "row_selection"
        - source: human-readable source name (e.g. "plotly", "vega")
        - source_id: model id of the originating component
        - timestamp: monotonic timestamp in ms
        - payload: original raw event from the library
        - selection: normalized selection data (when applicable)
            - mode: "point" | "range" | "rows"
            - indices: list of integer indices
            - values: list of {field: value} dicts
            - ranges: (mode="range" only) {axis: [min, max]}
        - viewport: normalized viewport data (when applicable)
            - ranges: {axis: [min, max]}
    """

    events = List(
        Dict(String, Any),
        default=[],
        help="""
        List of normalized interaction events. See class docstring for schema.
        """,
    )

    hover_events = List(
        Dict(String, Any),
        default=[],
        help="Filtered view: events of kind 'hover'",
    )

    selection_events = List(
        Dict(String, Any),
        default=[],
        help="Filtered view: events of kind 'selection'",
    )

    viewport_events = List(
        Dict(String, Any),
        default=[],
        help="Filtered view: events of kind 'viewport'",
    )

    row_selection_events = List(
        Dict(String, Any),
        default=[],
        help="Filtered view: events of kind 'row_selection'",
    )

    sources = Dict(
        String,
        String,
        default={},
        help="""
        Mapping from source model id to a human-readable source name.
        Components register themselves here when publishing their first event.
        """,
    )

    max_history = Int(
        default=100,
        help="Maximum number of events to keep in the history.",
    )

    _autoclear = Bool(
        default=False,
        help="If True, automatically clear events after they have been dispatched.",
    )

    __javascript_raw__: list[str] = []

    @classproperty
    def __javascript__(cls):
        return bundled_files(cls)

    @classproperty
    def __js_skip__(cls):
        return {}
