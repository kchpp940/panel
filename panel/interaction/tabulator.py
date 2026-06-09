from __future__ import annotations

import typing as t

from functools import partial

from ..io.state import state
from .base import AdapterEvent, InteractionAdapter, StandardEvent

if t.TYPE_CHECKING:
    from ..widgets.tables import Tabulator


class TabulatorAdapter(InteractionAdapter):
    """
    Interaction adapter for Tabulator widgets.

    **Owns the entire Tabulator event flow:**
      * selection-change  →  update remote-pagination selection
      * cell-click  →  resolve row/column, invoke callbacks
      * table-edit  →  handle pre/post edit, run filters check,
                       invoke edit callbacks, update styler
      * extracts filters (header + internal) and viewport (pagination,
        sorters) into the standardized event
    """

    _event_names: tuple[str, ...] = (
        'cell-click', 'table-edit', 'selection-change',
    )

    _KIND_MAP: dict[str, str] = {
        'cell-click': 'cell_click',
        'table-edit': 'cell_edit',
        'selection-change': 'selection',
    }

    def __init__(
        self,
        component: Tabulator,
        source_id: str | None = None,
        dataset_id: str | None = None,
        store=None,
    ) -> None:
        super().__init__(component, source_id, dataset_id, store)

    def get_kind(self, event: AdapterEvent) -> str:
        name = getattr(event.raw, 'event_name', event.event_name)
        return self._KIND_MAP.get(name, name)

    def _get_row_value(self, row_idx: int, column: str | None) -> t.Any:
        component = self._component
        if component.value is None:
            return None
        try:
            if column and column in component.value.columns:
                return component.value[column].iloc[row_idx]
            return component.value.index.iloc[row_idx]
        except (IndexError, AttributeError):
            return None

    def _extract_selection(self, event_name: str, raw: t.Any) -> dict[str, t.Any] | None:
        if event_name == 'selection-change':
            indices = getattr(raw, 'indices', None)
            if indices is not None:
                selected = getattr(raw, 'selected', True)
                flush = getattr(raw, 'flush', False)
                return {
                    'type': 'row',
                    'indexes': list(indices),
                    'selected': selected,
                    'flush': flush,
                    'field': 'index',
                }
            selection = getattr(self._component, 'selection', [])
            if selection:
                return {
                    'type': 'row',
                    'indexes': list(selection),
                    'field': 'index',
                }
            return None

        if event_name in ('cell-click', 'table-edit'):
            row = getattr(raw, 'row', None)
            column = getattr(raw, 'column', None)
            value = getattr(raw, 'value', None)
            if row is not None:
                return {
                    'type': 'cell',
                    'indexes': [row],
                    'field': column,
                    'value': value,
                }
        return None

    def _extract_filters(self) -> list[dict[str, t.Any]] | None:
        header_filters = getattr(self._component, 'filters', None)
        internal_filters = getattr(self._component, '_filters', None)
        filters: list[dict[str, t.Any]] = []
        if header_filters:
            filters.extend(header_filters)
        if internal_filters:
            for col_name, filt in internal_filters:
                if col_name is None:
                    continue
                filt_entry: dict[str, t.Any] = {'field': col_name}
                if isinstance(filt, tuple):
                    filt_entry['type'] = 'range'
                    filt_entry['min'], filt_entry['max'] = filt
                elif isinstance(filt, list):
                    filt_entry['type'] = 'in'
                    filt_entry['value'] = list(filt)
                else:
                    filt_entry['type'] = '='
                    filt_entry['value'] = filt
                filters.append(filt_entry)
        return filters or None

    def _extract_viewport(self) -> dict[str, t.Any] | None:
        pagination = getattr(self._component, 'pagination', None)
        if not pagination:
            return None
        page = getattr(self._component, 'page', 1)
        page_size = getattr(self._component, 'page_size', None)
        initial_page_size = getattr(self._component, 'initial_page_size', 20)
        ps = page_size or initial_page_size
        sorters = getattr(self._component, 'sorters', [])
        return {
            'page': page,
            'page_size': ps,
            'start': (page - 1) * ps,
            'end': page * ps,
            'sorters': list(sorters),
            'pagination': pagination,
        }

    def extract_payload(self, event: AdapterEvent) -> dict[str, t.Any]:
        raw = event.raw
        event_name = getattr(raw, 'event_name', event.event_name)

        result: dict[str, t.Any] = {'event_type': event_name}

        selection = self._extract_selection(event_name, raw)
        if selection:
            result['selection'] = selection

        if event_name in ('table-edit', 'cell-click'):
            result['_row'] = getattr(raw, 'row', None)
            result['_column'] = getattr(raw, 'column', None)
            result['_value'] = getattr(raw, 'value', None)

        if event_name == 'table-edit':
            result['_old'] = getattr(raw, 'old', None)
            result['_pre'] = getattr(raw, 'pre', False)

        filters = self._extract_filters()
        if filters:
            result['filters'] = filters

        viewport = self._extract_viewport()
        if viewport:
            result['viewport'] = viewport

        result['_raw_event'] = raw
        return result

    def on_standardized_event(self, event: StandardEvent, raw: AdapterEvent) -> None:
        comp = self._component
        raw_event = event.payload.get('_raw_event')
        if raw_event is None:
            return
        event_name = event.payload.get('event_type', '')

        if event_name == 'selection-change':
            if comp.pagination == 'remote':
                comp._update_selection(raw_event)
            return

        event_col = comp._renamed_cols.get(raw_event.column, raw_event.column)
        if comp.pagination == 'remote':
            nrows = comp.page_size or comp.initial_page_size
            raw_event.row = raw_event.row + (comp.page - 1) * nrows

        idx = comp._index_mapping.get(raw_event.row, raw_event.row)
        iloc = comp.value.index.get_loc(idx)
        comp._validate_iloc(idx, iloc)
        raw_event.row = iloc
        if event_col not in comp.buttons:
            if event_col in comp.value.columns:
                raw_event.value = comp.value[event_col].iloc[raw_event.row]
            else:
                raw_event.value = comp.value.index[raw_event.row]

        if event_name == 'table-edit':
            if raw_event.pre:
                import pandas as pd
                filter_df = pd.DataFrame({raw_event.column: [raw_event.value]})
                filters = comp._get_header_filters(filter_df)
                if filters and filters[0].any():
                    comp._edited_indexes.append(idx)
            else:
                if comp._old_value is not None:
                    raw_event.old = comp._old_value[event_col].iloc[raw_event.row]
                for cb in comp._on_edit_callbacks:
                    state.execute(partial(cb, raw_event), schedule=False)
                comp._update_style()
        else:
            for cb in comp._on_click_callbacks.get(None, []):
                state.execute(partial(cb, raw_event), schedule=False)
            for cb in comp._on_click_callbacks.get(event_col, []):
                state.execute(partial(cb, raw_event), schedule=False)

    def register_events(self, model, doc, comm=None) -> None:
        self._component._register_events(
            'cell-click', 'table-edit', 'selection-change',
            model=model, doc=doc, comm=comm
        )
