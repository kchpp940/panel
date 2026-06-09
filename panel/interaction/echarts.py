from __future__ import annotations

import typing as t

from .base import AdapterEvent, InteractionAdapter

if t.TYPE_CHECKING:
    from ..pane.echarts import ECharts


class EChartsAdapter(InteractionAdapter):
    """
    Interaction adapter for ECharts panes.

    **Responsibility (narrow):**
      * Map raw echarts event names to normalized ``kind`` strings.
      * Extract ``selection`` (click / selectchanged / legend) and
        ``viewport`` (datazoom ranges) from the raw event data.

    Does **not** execute any Python callbacks — those live in the
    ECharts pane itself.
    """

    _event_names: tuple[str, ...] = ('echarts_event',)

    _KIND_MAP: dict[str, str] = {
        'click': 'point_click',
        'dblclick': 'point_doubleclick',
        'mousedown': 'point_mousedown',
        'mouseup': 'point_mouseup',
        'mouseover': 'point_hover',
        'mouseout': 'point_leave',
        'selectchanged': 'selection',
        'legendselectchanged': 'legend_selection',
        'legendselected': 'legend_select',
        'legendunselected': 'legend_deselect',
        'datazoom': 'viewport_change',
        'datarangeselected': 'data_range_selection',
        'timelineplaychanged': 'timeline_change',
        'restore': 'restore',
        'brush': 'brush',
        'brushEnd': 'brush_end',
        'geoselectchanged': 'geo_selection',
        'axisareaselected': 'axis_area_selection',
    }

    def __init__(
        self,
        component: ECharts,
        source_id: str | None = None,
        dataset_id: str | None = None,
        store=None,
    ) -> None:
        super().__init__(component, source_id, dataset_id, store)

    def get_kind(self, event: AdapterEvent) -> str:
        etype = getattr(event.raw, 'type', event.event_name)
        return self._KIND_MAP.get(etype, etype)

    def _extract_selection(self, etype: str, data: dict[str, t.Any]) -> dict[str, t.Any] | None:
        if not data:
            return None

        selection: dict[str, t.Any] = {}

        if etype in ('click', 'dblclick', 'mousedown', 'mouseup', 'mouseover'):
            if 'dataIndex' in data:
                selection['indexes'] = [data['dataIndex']]
            for key in ('seriesIndex', 'seriesName', 'name', 'value', 'data'):
                if key in data:
                    snake = 'series_index' if key == 'seriesIndex' else key
                    selection[snake] = data[key]

        elif etype == 'selectchanged':
            selected = data.get('selected', [])
            indexes: list[int] = []
            for item in selected:
                if isinstance(item, dict) and 'dataIndex' in item:
                    if isinstance(item['dataIndex'], list):
                        indexes.extend(item['dataIndex'])
                    else:
                        indexes.append(item['dataIndex'])
                elif isinstance(item, int):
                    indexes.append(item)
            if indexes:
                selection['indexes'] = indexes
            selection['selected'] = selected
            selection['isFromClick'] = data.get('isFromClick', False)

        elif etype == 'legendselectchanged':
            selection['type'] = 'legend'
            selection['selected'] = data.get('selected', {})
            selection['name'] = data.get('name')

        if not selection:
            return None
        selection['event_type'] = etype
        return selection

    def _extract_viewport(self, etype: str, data: dict[str, t.Any]) -> dict[str, t.Any] | None:
        if etype != 'datazoom' or not data:
            return None
        viewport: dict[str, t.Any] = {}
        for batch in data.get('batch', []):
            xidx = batch.get('xAxisIndex', 0)
            if 'startValue' in batch or 'endValue' in batch:
                viewport[f'xAxisIndex_{xidx}'] = {
                    'start': batch.get('startValue'),
                    'end': batch.get('endValue'),
                }
            if 'start' in batch or 'end' in batch:
                viewport[f'xAxisIndex_{xidx}_percent'] = {
                    'start': batch.get('start'),
                    'end': batch.get('end'),
                }
        return viewport or None

    def extract_payload(self, event: AdapterEvent) -> dict[str, t.Any]:
        raw = event.raw
        etype = getattr(raw, 'type', '')
        data = getattr(raw, 'data', None)
        query = getattr(raw, 'query', None)

        result: dict[str, t.Any] = {'event_type': etype, 'raw_data': data}
        if query is not None:
            result['query'] = query

        selection = self._extract_selection(etype, data or {})
        if selection:
            result['selection'] = selection

        viewport = self._extract_viewport(etype, data or {})
        if viewport:
            result['viewport'] = viewport

        return result
