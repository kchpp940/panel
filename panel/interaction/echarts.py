from __future__ import annotations

import typing as t

from .base import AdapterEvent, InteractionAdapter

if t.TYPE_CHECKING:
    from ..pane.echarts import ECharts


class EChartsAdapter(InteractionAdapter):
    """
    Interaction adapter for ECharts panes.

    Handles echarts_event messages (click, selectchanged, datazoom,
    etc.) and extracts selection, viewport and payload.
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
        raw = event.raw
        etype = getattr(raw, 'type', event.event_name)
        return self._KIND_MAP.get(etype, etype)

    def _extract_selection(self, etype: str, data: dict[str, t.Any]) -> dict[str, t.Any] | None:
        if not data:
            return None

        selection: dict[str, t.Any] = {}

        if etype in ('click', 'dblclick', 'mousedown', 'mouseup', 'mouseover'):
            if 'dataIndex' in data:
                selection['indexes'] = [data['dataIndex']]
            if 'seriesIndex' in data:
                selection['series_index'] = data['seriesIndex']
            if 'seriesName' in data:
                selection['series_name'] = data['seriesName']
            if 'name' in data:
                selection['name'] = data['name']
            if 'value' in data:
                selection['value'] = data['value']
            if 'data' in data:
                selection['data'] = data['data']

        elif etype == 'selectchanged':
            selected = data.get('selected', [])
            if selected:
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

        if selection:
            selection['event_type'] = etype
            return selection
        return None

    def _extract_viewport(self, etype: str, data: dict[str, t.Any]) -> dict[str, t.Any] | None:
        if etype != 'datazoom' or not data:
            return None
        viewport: dict[str, t.Any] = {}
        for batch in data.get('batch', []):
            if 'startValue' in batch or 'endValue' in batch:
                axis_key = f"xAxisIndex_{batch.get('xAxisIndex', 0)}"
                viewport[axis_key] = {
                    'start': batch.get('startValue'),
                    'end': batch.get('endValue'),
                }
            if 'start' in batch or 'end' in batch:
                axis_key = f"xAxisIndex_{batch.get('xAxisIndex', 0)}_percent"
                viewport[axis_key] = {
                    'start': batch.get('start'),
                    'end': batch.get('end'),
                }
        return viewport or None

    def extract_payload(self, event: AdapterEvent) -> dict[str, t.Any]:
        raw = event.raw
        etype = getattr(raw, 'type', '')
        data = getattr(raw, 'data', None)
        query = getattr(raw, 'query', None)

        result: dict[str, t.Any] = {'event_type': etype}
        if query is not None:
            result['query'] = query

        selection = self._extract_selection(etype, data or {})
        if selection:
            result['selection'] = selection

        viewport = self._extract_viewport(etype, data or {})
        if viewport:
            result['viewport'] = viewport

        result['raw_data'] = data
        return result

    def register_events(self, model, doc, comm=None) -> None:
        self._component._register_events('echarts_event', model=model, doc=doc, comm=comm)
