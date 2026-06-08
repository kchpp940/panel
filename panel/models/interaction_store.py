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

InteractionEventType = t.Literal["hover", "selection", "viewport", "row_selection"]

InteractionEventTypes = enumeration("hover", "selection", "viewport", "row_selection")


class InteractionEvent(ModelEvent):

    event_name = 'interaction_event'

    def __init__(
        self,
        model,
        type: InteractionEventType | None = None,
        source: str | None = None,
        data: t.Any = None,
    ):
        self.type = type
        self.source = source
        self.data = data
        super().__init__(model=model)

    def __repr__(self):
        return (
            f'{type(self).__name__}(type={self.type!r}, source={self.source!r}, '
            f'data={self.data!r})'
        )


class InteractionStore(Model):
    """
    A centralized store for collecting and distributing interaction events
    across multiple Panel components (Plotly, Vega, ECharts, Tabulator, etc.).

    Components publish their events to the store, and other components or
    Python callbacks can subscribe to receive filtered events by type
    or source.
    """

    events = List(
        Dict(String, Any),
        default=[],
        help="""
        List of interaction events. Each event is a dict with keys:
        - type: 'hover' | 'selection' | 'viewport' | 'row_selection'
        - source: model id of the originating component
        - data: event-specific payload
        - timestamp: monotonic timestamp in ms
        """,
    )

    hover_events = List(
        Dict(String, Any),
        default=[],
        help="Filtered view: events of type 'hover'",
    )

    selection_events = List(
        Dict(String, Any),
        default=[],
        help="Filtered view: events of type 'selection'",
    )

    viewport_events = List(
        Dict(String, Any),
        default=[],
        help="Filtered view: events of type 'viewport'",
    )

    row_selection_events = List(
        Dict(String, Any),
        default=[],
        help="Filtered view: events of type 'row_selection'",
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
