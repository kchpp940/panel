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
    from bokeh.models import ColumnDataSource
    from pandas import DataFrame
    from pyviz_comms import Comm

    InteractionEventKind = t.Literal["hover", "selection", "viewport", "row_selection"]
    InteractionBindMode = t.Literal["highlight", "filter"]


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
# Internal: target-type helpers
# ---------------------------------------------------------------------------

_BINDING_KEY = t.Tuple[
    t.Any,                # target
    str,                  # mode
    frozenset[str] | None,  # exclude_source_ids
]


def _extract_dataset_id(event: dict[str, t.Any]) -> str | None:
    """Return the dataset_id embedded in a normalized event, or None."""
    for section in ("selection", "viewport"):
        sec = event.get(section)
        if isinstance(sec, dict) and sec.get("dataset_id"):
            return sec["dataset_id"]
    return None


def _extract_event_filters(event: dict[str, t.Any]) -> list[InteractionFilter]:
    """Return the filters list from selection/viewport (selection wins)."""
    sel = event.get("selection")
    if isinstance(sel, dict) and sel.get("filters"):
        return list(sel["filters"])
    vp = event.get("viewport")
    if isinstance(vp, dict) and vp.get("filters"):
        return list(vp["filters"])
    return []


def _target_source_ids(target: t.Any) -> set[str]:
    """
    Infer the Bokeh model id(s) for a Panel Reactive target (e.g.
    Tabulator, Plotly pane). For non-Reactive targets returns an empty
    set so no self-exclusion is performed automatically.
    """
    ids: set[str] = set()
    models = getattr(target, "_models", None)
    if isinstance(models, dict):
        for ref_id in models.keys():
            if isinstance(ref_id, str):
                ids.add(ref_id)
    return ids


def _get_target_df(target: t.Any) -> DataFrame | None:
    """
    Best-effort extract a pandas DataFrame from a bound target.
    Supports Tabulator (``value`` / ``_processed_data``) and any
    target that exposes a ``.value`` DataFrame attribute.
    """
    import pandas as pd
    value = getattr(target, "value", None)
    if isinstance(value, pd.DataFrame):
        return value
    processed = getattr(target, "_processed_data", None)
    if isinstance(processed, pd.DataFrame):
        return processed
    return None


def _get_target_cds(target: t.Any) -> ColumnDataSource | None:
    """
    Best-effort return the Bokeh ColumnDataSource driving a bound target.
    Supports targets that store their Bokeh model in ``_models``, as
    well as raw ColumnDataSource instances.
    """
    from bokeh.models import ColumnDataSource
    if isinstance(target, ColumnDataSource):
        return target
    models = getattr(target, "_models", None)
    if isinstance(models, dict):
        for model, _parent in models.values():
            source = getattr(model, "source", None)
            if isinstance(source, ColumnDataSource):
                return source
    return None


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

    Automatic cross-component binding
    ---------------------------------
    Beyond simple pub/sub, ``InteractionStore`` also supports **binding**
    targets (Tabulator widgets, ColumnDataSources, or plain Python
    callables) to a ``dataset_id``. When an event with a matching
    ``dataset_id`` arrives, the store automatically applies the event's
    ``filters`` to each bound target. The component that *originated*
    the event is excluded from receiving the update, preventing
    feedback loops.

    Two binding modes are available:

    * ``"highlight"`` (default) — update the target's
      ``selected.indices`` so the matching rows are highlighted but all
      original data stays visible.
    * ``"filter"`` — replace the target's visible data with only the
      rows that match the filters. Supported for Tabulator (updates
      ``.value``).

    Example::

        >>> import pandas as pd
        >>> import panel as pn
        >>>
        >>> df = pd.DataFrame({"x": [1, 2, 3, 4, 5], "y": [10, 20, 30, 40, 50]})
        >>> store = pn.io.InteractionStore()
        >>>
        >>> plot = pn.pane.Plotly(
        ...     px.scatter(df, x="x", y="y"),
        ...     interaction_store=store,
        ...     interaction_fields=["x", "y"],
        ... )
        >>> table = pn.widgets.Tabulator(df, interaction_store=store)
        >>>
        >>> # Two-way highlight linkage: selecting in the plot highlights
        >>> # rows in the table and vice versa, without echoing back.
        >>> store.bind("default", table, mode="highlight")
        >>> store.bind("default", plot,  mode="highlight")
        >>>
        >>> # You can also bind a plain Python callable for arbitrary logic:
        >>> def on_change(event, filtered_df):
        ...     print(f"{len(filtered_df)} rows match")
        >>> store.bind("default", on_change)

    Reference: https://panel.holoviz.org/api/panel.io.InteractionStore.html
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
        # dataset_id -> list of binding dicts:
        #   {"target": ..., "mode": "highlight"|"filter",
        #    "exclude_source_ids": frozenset|None}
        self._bindings: dict[str, list[dict[str, t.Any]]] = {}
        self._internal_callbacks.append(
            self.param.watch(self._notify_subscribers, ['events'])
        )

    # ------------------------------------------------------------------
    # Pub/sub
    # ------------------------------------------------------------------

    def _notify_subscribers(self, *events: param.parameterized.Event) -> None:
        if not self.events:
            return
        latest = self.events[-1]
        # Deliver to manual subscribers first.
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
        # Then auto-apply to any bound targets.
        try:
            self._apply_event_to_bindings(latest)
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

    # ------------------------------------------------------------------
    # Binding API
    # ------------------------------------------------------------------

    def bind(
        self,
        dataset_id: str,
        target: t.Any,
        mode: InteractionBindMode = "highlight",
        exclude_source_ids: str | list[str] | None = None,
        dataframe: DataFrame | None = None,
    ) -> Callable[[], None]:
        """
        Bind a target to a dataset id so that matching events are
        automatically applied.

        When an event whose ``selection.dataset_id`` or
        ``viewport.dataset_id`` equals ``dataset_id`` arrives, the
        store applies the event's ``filters`` to ``target``. The
        target that originated the event is automatically excluded
        from receiving the update (so selections do not echo back to
        the source component).

        Supported targets
        -----------------
        * **Tabulator widget** — in ``"highlight"`` mode the table's
          ``selected.indices`` are updated; in ``"filter"`` mode the
          widget's ``.value`` DataFrame is replaced with the filtered
          rows.
        * **bokeh.models.ColumnDataSource** — in ``"highlight"`` mode
          the CDS's ``selected.indices`` are updated.
        * **callable** — invoked with ``callback(event, filtered_df)``.
          If the callable takes only one argument it is passed just the
          event dict for backwards compatibility with
          :meth:`subscribe`. When the callable is a plain function (not
          a Panel widget) you should pass the ``dataframe`` argument so
          the filtered DataFrame can be computed.

        Parameters
        ----------
        dataset_id : str
            Match events whose ``selection.dataset_id`` or
            ``viewport.dataset_id`` equals this value.
        target : Tabulator | ColumnDataSource | callable
            The object to bind.
        mode : "highlight" or "filter", default "highlight"
            How to apply the filters to the target.
        exclude_source_ids : str or list[str], optional
            Additional source model id(s) to exclude *in addition* to
            the automatic self-exclusion performed for Reactive targets.
        dataframe : pandas.DataFrame, optional
            The source DataFrame to filter when ``target`` is a plain
            callable that does not expose its own data. Ignored for
            Tabulator / ColumnDataSource targets.

        Returns
        -------
        unbind : callable
            A zero-argument function that removes the binding when
            called.
        """
        if mode not in ("highlight", "filter"):
            raise ValueError(
                f"mode must be 'highlight' or 'filter', got {mode!r}"
            )

        explicit_exclude: set[str] = set()
        if isinstance(exclude_source_ids, str):
            explicit_exclude.add(exclude_source_ids)
        elif isinstance(exclude_source_ids, list):
            explicit_exclude.update(exclude_source_ids)
        # Auto-exclude the target's own model ids if it is a Reactive.
        auto_exclude = _target_source_ids(target)
        all_exclude = explicit_exclude | auto_exclude
        exclude_frozen = frozenset(all_exclude) if all_exclude else None

        entry: dict[str, t.Any] = {
            "target": target,
            "mode": mode,
            "exclude_source_ids": exclude_frozen,
            "dataframe": dataframe,
        }
        self._bindings.setdefault(dataset_id, []).append(entry)

        def unbind() -> None:
            entries = self._bindings.get(dataset_id)
            if entries is None:
                return
            if entry in entries:
                entries.remove(entry)
            if not entries:
                del self._bindings[dataset_id]

        return unbind

    def unbind(
        self,
        dataset_id: str,
        target: t.Any | None = None,
    ) -> None:
        """
        Remove one or all bindings for a dataset id.

        Parameters
        ----------
        dataset_id : str
        target : object, optional
            If provided, only bindings for this specific target are
            removed; otherwise *all* bindings for ``dataset_id`` are
            removed.
        """
        entries = self._bindings.get(dataset_id)
        if entries is None:
            return
        if target is None:
            del self._bindings[dataset_id]
            return
        self._bindings[dataset_id] = [
            e for e in entries if e["target"] is not target
        ]
        if not self._bindings[dataset_id]:
            del self._bindings[dataset_id]

    def get_bindings(
        self,
        dataset_id: str | None = None,
    ) -> dict[str, list[dict[str, t.Any]]]:
        """
        Return the current bindings.

        Parameters
        ----------
        dataset_id : str, optional
            If provided, only the bindings for this dataset id are
            returned (as ``{dataset_id: [...]}``). Otherwise a dict
            mapping every registered dataset id to its bindings is
            returned.

        Returns
        -------
        bindings : dict
        """
        if dataset_id is None:
            return dict(self._bindings)
        if dataset_id in self._bindings:
            return {dataset_id: list(self._bindings[dataset_id])}
        return {}

    # ------------------------------------------------------------------
    # Internal: event → bindings dispatcher
    # ------------------------------------------------------------------

    def _apply_event_to_bindings(self, event: dict[str, t.Any]) -> None:
        """
        Dispatch an incoming event to every bound target whose
        ``dataset_id`` matches (and whose ``exclude_source_ids`` does
        not contain the event's source).
        """
        dataset_id = _extract_dataset_id(event)
        if dataset_id is None:
            return
        entries = self._bindings.get(dataset_id)
        if not entries:
            return
        filters = _extract_event_filters(event)
        source_id = event.get("source_id")
        for entry in entries:
            excluded = entry["exclude_source_ids"]
            if excluded is not None and source_id is not None and source_id in excluded:
                continue
            try:
                self._apply_to_target(
                    entry["target"], entry["mode"], event, filters,
                    bound_dataframe=entry.get("dataframe"),
                )
            except Exception:
                import traceback
                traceback.print_exc()

    def _apply_to_target(
        self,
        target: t.Any,
        mode: InteractionBindMode,
        event: dict[str, t.Any],
        filters: list[InteractionFilter],
        bound_dataframe: DataFrame | None = None,
    ) -> None:
        """Dispatch a single (event, filters) pair to a specific target."""
        from bokeh.models import ColumnDataSource
        from inspect import signature

        # Case 1: plain Python callable (not a param Reactive with .param).
        if callable(target) and not hasattr(target, "param"):
            try:
                sig = signature(target)
                nparams = len([
                    p for p in sig.parameters.values()
                    if p.default is p.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
                ])
            except (TypeError, ValueError):
                nparams = 1
            if nparams >= 2:
                df = self._build_filtered_df_for_target(
                    target, filters, prefer_dataframe=bound_dataframe,
                )
                target(event, df)
            else:
                target(event)
            return

        # Case 2: ColumnDataSource (raw or via a model like Tabulator).
        cds = _get_target_cds(target)
        if cds is not None:
            if mode == "highlight":
                indices = self._compute_matching_indices(cds, filters)
                cds.selected.indices = indices
                return
            if mode == "filter":
                # Filter mode on a CDS is a potentially destructive
                # operation on shared state; fall through to see if the
                # wrapping target exposes a higher-level value API.
                pass

        # Case 3: targets exposing a .value DataFrame (e.g. Tabulator).
        df = _get_target_df(target)
        if df is not None:
            filtered = apply_filters_to_df(df, filters) if filters else df
            if mode == "filter":
                target.value = filtered
            elif mode == "highlight":
                indices = list(filtered.index) if filters else list(df.index)
                if (
                    hasattr(target, "selection")
                    and isinstance(getattr(type(target), "selection", None), param.Parameter)
                ):
                    target.selection = indices
            return

    @staticmethod
    def _build_filtered_df_for_target(
        target: t.Any,
        filters: list[InteractionFilter],
        prefer_dataframe: DataFrame | None = None,
    ) -> DataFrame | None:
        """
        Try to build a filtered DataFrame. If ``prefer_dataframe`` is
        supplied (as passed to :meth:`bind`) it is used as the source;
        otherwise the method inspects ``target`` for a ``.value``
        DataFrame or underlying ColumnDataSource.
        """
        import pandas as pd
        df = prefer_dataframe
        if df is None:
            df = _get_target_df(target)
        if df is None:
            cds = _get_target_cds(target)
            if cds is not None and cds.data:
                try:
                    df = pd.DataFrame(cds.data)
                except Exception:
                    df = None
        if df is None:
            return None
        return apply_filters_to_df(df, filters) if filters else df

    @staticmethod
    def _compute_matching_indices(
        cds: ColumnDataSource,
        filters: list[InteractionFilter],
    ) -> list[int]:
        """
        Convert a list of InteractionFilters to a list of integer row
        indices compatible with ``ColumnDataSource.selected.indices``.
        When no filters are provided returns an empty list (clearing the
        selection).
        """
        import pandas as pd
        if not filters or not cds.data:
            return []
        try:
            df = pd.DataFrame(cds.data)
        except Exception:
            return []
        filtered = apply_filters_to_df(df, filters)
        return [int(i) for i in filtered.index.tolist()]

    # ------------------------------------------------------------------
    # Bokeh plumbing
    # ------------------------------------------------------------------

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
