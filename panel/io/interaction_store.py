"""
Defines the InteractionStore component which provides a centralized store
for cross-component interaction events (hover, selection, viewport, row selection).
"""
from __future__ import annotations

import typing as t

import param

from ..models.interaction_store import (
    InteractionFilter,
    InteractionEvent as _BkInteractionEvent,
    InteractionStore as _BkInteractionStore,
)
from ..reactive import Reactive
from .document import create_doc_if_none_exists
from .state import state

if t.TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from bokeh.document import Document
    from bokeh.model import Model
    from pandas import DataFrame
    from pyviz_comms import Comm

    InteractionEventKind = t.Literal["hover", "selection", "viewport", "row_selection"]


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def apply_filter(
    df: DataFrame,
    f: InteractionFilter,
) -> DataFrame:
    """
    Apply a single ``InteractionFilter`` to a pandas DataFrame.

    Parameters
    ----------
    df : pandas.DataFrame
        The dataframe to filter.
    f : dict
        An InteractionFilter dict with keys ``field``, ``op``, ``value``.

    Returns
    -------
    DataFrame
        A filtered view/copy of the input dataframe.
    """
    field = f["field"]
    op = f["op"]
    value = f["value"]

    if field == "index":
        series = df.index
    elif field in df.columns:
        series = df[field]
    else:
        return df.iloc[0:0]

    if op == "in":
        mask = series.isin(list(value))
    elif op == "not_in":
        mask = ~series.isin(list(value))
    elif op == "range":
        lo, hi = value
        mask = (series >= lo) & (series <= hi)
    elif op == "==":
        mask = series == value
    elif op == "!=":
        mask = series != value
    elif op == ">":
        mask = series > value
    elif op == ">=":
        mask = series >= value
    elif op == "<":
        mask = series < value
    elif op == "<=":
        mask = series <= value
    else:
        raise ValueError(f"Unknown filter op: {op!r}")

    return df[mask]


def apply_filters_to_df(
    df: DataFrame,
    filters: list[InteractionFilter],
) -> DataFrame:
    """
    Apply a list of normalized ``InteractionFilter`` dicts to a pandas
    DataFrame. Filters are combined with AND (logical conjunction).

    The ``filters`` list comes directly from an event's
    ``event['selection']['filters']`` or ``event['viewport']['filters']``
    so subscribers do not need to understand library-specific payloads.

    Parameters
    ----------
    df : pandas.DataFrame
        The dataframe to filter.
    filters : list[dict]
        A list of filter dicts of the form
        ``{field: str, op: str, value: any}``.

    Returns
    -------
    DataFrame
        The filtered dataframe. If ``filters`` is empty the original
        dataframe is returned.

    Example
    -------

    >>> store = pn.io.InteractionStore()
    >>> plotly = pn.pane.Plotly(fig, interaction_store=store)
    >>> def on_selection(event):
    ...     if event['selection'] is not None:
    ...         filtered = pn.io.apply_filters_to_df(df, event['selection']['filters'])
    ...         print(filtered)
    >>> store.subscribe(on_selection, kind="selection")
    """
    if not filters:
        return df
    result = df
    for f in filters:
        result = apply_filter(result, f)
    return result


def apply_event_filters_to_df(
    df: DataFrame,
    event: dict[str, t.Any],
) -> DataFrame:
    """
    Convenience wrapper over :func:`apply_filters_to_df` that takes a
    full normalized event dict and applies ``filters`` from either
    ``event['selection']['filters']`` or ``event['viewport']['filters']``
    (preference given to selection filters if both are present).

    Parameters
    ----------
    df : pandas.DataFrame
    event : dict
        A normalized InteractionEvent payload.

    Returns
    -------
    DataFrame
    """
    filters: list[InteractionFilter] = []
    if event.get("selection") and event["selection"].get("filters"):
        filters = event["selection"]["filters"]
    elif event.get("viewport") and event["viewport"].get("filters"):
        filters = event["viewport"]["filters"]
    return apply_filters_to_df(df, filters)


# ---------------------------------------------------------------------------
# InteractionStore component
# ---------------------------------------------------------------------------

class InteractionStore(Reactive):
    """
    The InteractionStore provides a centralized store for collecting and
    distributing interaction events across multiple Panel components such
    as Plotly, Vega, ECharts, and Tabulator.

    Components publish their interaction events to the store, and Python
    callbacks or other components can subscribe to receive events filtered
    by kind or source. Each event has a **normalized schema** so subscribers
    do **not** need to handle library-specific data structures.

    Normalized event schema
    -----------------------
    Every event in ``events`` (and every payload delivered to a subscriber
    callback) has the following shape::

        {
            "kind":        "hover" | "selection" | "viewport" | "row_selection",
            "source":      "plotly" | "vega" | "echarts" | "tabulator" | ...,
            "source_id":   "<bokeh model id>",
            "timestamp":   1710000000000,
            "payload":     <original raw event from the library>,
            "selection":   <see below>,     # for selection / hover / row_selection
            "viewport":    <see below>,     # for viewport events
        }

    ``selection`` structure (when present)::

        {
            "mode":        "point" | "range" | "rows",
            "dataset_id":  "<optional dataset/source name>",
            "indices":     [0, 5, 7, ...],           # selected row indices
            "fields":      ["x", "y", "category"],   # column/field names involved
            "values":      [{col: val, ...}, ...],   # per-row column values
            "ranges":      {"x": [0, 10], ...},      # only for mode="range"
            "filters":     [                          # ready-made filter objects
                {"field": "x", "op": "in", "value": [1, 3]},
                {"field": "index", "op": "in", "value": [0, 5]},
                {"field": "y", "op": "range", "value": [-5, 5]},
                ...
            ],
        }

    ``viewport`` structure (when present)::

        {
            "dataset_id": "<optional dataset/source name>",
            "fields":     ["xaxis", "yaxis"],
            "ranges":     {"xaxis": [0, 100], "yaxis": [-20, 20]},
            "filters":    [
                {"field": "x", "op": "range", "value": [0, 100]},
                ...
            ],
        }

    The ``filters`` list is designed to be passed directly to
    :func:`apply_filters_to_df` so you never need to parse Plotly/Vega/
    ECharts/Tabulator payloads by hand.

    Reference: https://panel.holoviz.org/api/panel.io.InteractionStore.html

    :Example:

    >>> import pandas as pd
    >>> import panel as pn
    >>>
    >>> df = pd.DataFrame({"x": [1, 2, 3, 4, 5], "y": [2, 4, 6, 8, 10]})
    >>> store = pn.io.InteractionStore()
    >>> table = pn.widgets.Tabulator(df, interaction_store=store)
    >>>
    >>> def on_any_selection(event):
    ...     if event.get("selection") or event.get("viewport"):
    ...         filtered = pn.io.apply_event_filters_to_df(df, event)
    ...         print("Filtered rows:", filtered)
    >>>
    >>> store.subscribe(on_any_selection, kind="selection")
    >>> store.subscribe(on_any_selection, kind="row_selection")
    """

    events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="""
        List of all normalized interaction events. See class docstring for
        the full schema.
        """,
    )

    hover_events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="Filtered view: events of kind 'hover'",
    )

    selection_events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="Filtered view: events of kind 'selection'",
    )

    viewport_events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="Filtered view: events of kind 'viewport'",
    )

    row_selection_events = param.List(
        default=[],
        item_type=dict,
        nested_refs=True,
        doc="Filtered view: events of kind 'row_selection'",
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
        for callback, kind_filter, source_filter in self._subscribers:
            if kind_filter is not None and latest['kind'] not in kind_filter:
                continue
            if source_filter is not None and latest['source_id'] not in source_filter:
                continue
            try:
                callback(latest)
            except Exception:
                import traceback
                traceback.print_exc()

    def subscribe(
        self,
        callback: Callable[[dict[str, t.Any]], None],
        kind: InteractionEventKind | list[InteractionEventKind] | None = None,
        source_id: str | list[str] | None = None,
    ) -> Callable[[], None]:
        """
        Subscribe to interaction events.

        Parameters
        ----------
        callback : callable
            A function that receives a single normalized event dict.
            See class docstring for the event schema.
        kind : str or list[str], optional
            Filter to only receive events of the given kind(s).
            One of: "hover", "selection", "viewport", "row_selection".
        source_id : str or list[str], optional
            Filter to only receive events from the given source model id(s).

        Returns
        -------
        unsubscribe : callable
            A function that when called removes the subscription.
        """
        kind_set = None if kind is None else (
            set(kind) if isinstance(kind, list) else {kind}
        )
        source_set = None if source_id is None else (
            set(source_id) if isinstance(source_id, list) else {source_id}
        )
        entry = (callback, kind_set, source_set)
        self._subscribers.append(entry)

        def unsubscribe() -> None:
            if entry in self._subscribers:
                self._subscribers.remove(entry)

        return unsubscribe

    def clear(
        self,
        kind: InteractionEventKind | None = None,
        source_id: str | None = None,
    ) -> None:
        """
        Clear events from the store.

        Parameters
        ----------
        kind : str, optional
            If given, only clear events of this kind.
        source_id : str, optional
            If given, only clear events from this source model id.
        """
        if kind is not None or source_id is not None:
            filtered = [
                e for e in self.events
                if (kind is None or e['kind'] != kind)
                and (source_id is None or e['source_id'] != source_id)
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
        kind: InteractionEventKind | None = None,
        source_id: str | None = None,
    ) -> list[dict[str, t.Any]]:
        """
        Retrieve events filtered by kind and/or source.

        Parameters
        ----------
        kind : str, optional
            Only return events of this kind.
        source_id : str, optional
            Only return events from this source model id.

        Returns
        -------
        events : list[dict]
            A list of matching normalized event dicts.
        """
        return [
            e for e in self.events
            if (kind is None or e['kind'] == kind)
            and (source_id is None or e['source_id'] == source_id)
        ]

    def apply_to_df(self, df: DataFrame) -> DataFrame:
        """
        Apply filters from the *latest* event in the store to a pandas
        DataFrame. Equivalent to::

            pn.io.apply_event_filters_to_df(df, self.events[-1])

        Parameters
        ----------
        df : pandas.DataFrame

        Returns
        -------
        pandas.DataFrame
        """
        if not self.events:
            return df
        return apply_event_filters_to_df(df, self.events[-1])

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
