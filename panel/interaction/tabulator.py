from __future__ import annotations

import typing as t

from .base import AdapterEvent, InteractionAdapter

if t.TYPE_CHECKING:
    from ..widgets.tables import Tabulator


class TabulatorAdapter(InteractionAdapter):
    """
    Interaction adapter for Tabulator widgets.

    Handles cell-click, table-edit, selection-change and filter events
    and extracts selection, filters, viewport and payload.
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

        if event_name == 'table-edit':
            old = getattr(raw, 'old', None)
            value = getattr(raw, 'value', None)
            row = getattr(raw, 'row', None)
            column = getattr(raw, 'column', None)
            pre = getattr(raw, 'pre', False)
            result['edit'] = {
                'row': row,
                'column': column,
                'old': old,
                'value': value,
                'pre': pre,
            }

        if event_name == 'cell-click':
            row = getattr(raw, 'row', None)
            column = getattr(raw, 'column', None)
            value = getattr(raw, 'value', None)
            result['click'] = {
                'row': row,
                'column': column,
                'value': value,
            }

        filters = self._extract_filters()
        if filters:
            result['filters'] = filters

        viewport = self._extract_viewport()
        if viewport:
            result['viewport'] = viewport

        return result

    def register_events(self, model, doc, comm=None) -> None:
        self._component._register_events(
            'cell-click', 'table-edit', 'selection-change',
            model=model, doc=doc, comm=comm
        )
