"""
Unified interaction event adapters for Panel visualization components.

This module provides a standardized layer on top of the raw,
component-specific event protocols exposed by Plotly, Vega/Vega-Lite,
ECharts, and Tabulator.  Every component that has an interaction
adapter publishes **normalized** :class:`StandardEvent` objects to a
per-component :class:`InteractionStore`, which external consumers can
subscribe to without having to learn the quirks of each front-end
library.

Consumer API
------------

Every supported component (``panel.pane.Plotly``, ``panel.pane.Vega``,
``panel.pane.ECharts``, ``panel.widgets.Tabulator``) exposes three
public members for consuming standardized events:

* ``.interaction_store`` — the per-component :class:`InteractionStore`
  instance.
* ``.subscribe_interaction(callback, *, kind=None)`` — register a
  callback; ``kind`` filters by normalized event type (see below).
* ``.unsubscribe_interaction(callback, *, kind=None)`` — remove a
  previously registered callback.

The callback receives a single :class:`StandardEvent` argument with the
following fields:

``kind``
    Normalized event type, e.g. ``"point_click"``, ``"selection"``,
    ``"viewport_change"``, ``"cell_edit"``.
``source_id``
    Unique identifier of the originating component.
``dataset_id``
    Optional dataset identifier (set by the adapter when available).
``selection``
    Normalized selection dict.  Always contains at least an
    ``indexes`` key (list of integer indices) plus component-specific
    fields such as ``field``, ``trace_indexes``, ``xs``/``ys``,
    ``min``/``max``, ``values``, etc.
``viewport``
    Normalized viewport dict — axis ranges for Plotly/ECharts,
    pagination + sorters for Tabulator.
``filters``
    Auto-derived list of filter specifications usable with Panel
    data-transforms; generated from ``selection`` when possible.
``payload``
    Any extra fields the adapter extracted that do not fit into the
    categories above.
``event_id`` / ``timestamp``
    Auto-generated unique id and POSIX timestamp.

Quick example
-------------

::

    import plotly.express as px
    from panel.pane import Plotly

    df = px.data.iris()
    fig = px.scatter(df, x="sepal_width", y="sepal_length", color="species")
    plot = Plotly(fig)

    def on_any(event):
        print(f"[{event.kind}] selection={event.selection}  filters={event.filters}")

    def only_clicks(event):
        # runs for point_click events only
        idx = event.selection["indexes"]
        print(f"clicked rows: {df.iloc[idx]}")

    plot.subscribe_interaction(on_any)
    plot.subscribe_interaction(only_clicks, kind="point_click")

    # later:
    # plot.unsubscribe_interaction(on_any)
    # last = plot.interaction_store.get_last("point_click")

Adapter architecture
--------------------

Each backend library has a dedicated :class:`InteractionAdapter`
subclass whose **only** responsibility is to extract a payload from
the raw Bokeh event and map event-specific names to the standard
vocabulary.  All side effects — updating component parameters,
dispatching user callbacks, writing back selections, etc. — remain
inside the component itself so the adapter never owns business logic.

Each adapter owns its own :class:`InteractionStore`; stores are never
shared across components or sessions.
"""
from __future__ import annotations

from .base import (
    AdapterEvent, InteractionAdapter, InteractionStore,
    StandardEvent,
)
from .plotly import PlotlyAdapter
from .vega import VegaAdapter
from .echarts import EChartsAdapter
from .tabulator import TabulatorAdapter

__all__ = (
    'AdapterEvent', 'EChartsAdapter', 'InteractionAdapter',
    'InteractionStore', 'PlotlyAdapter', 'StandardEvent',
    'TabulatorAdapter', 'VegaAdapter',
)
