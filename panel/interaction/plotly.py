from __future__ import annotations

import typing as t

from .base import AdapterEvent, InteractionAdapter

if t.TYPE_CHECKING:
    from ..pane.plotly import Plotly


class PlotlyAdapter(InteractionAdapter):
    """
    Interaction adapter for Plotly panes.

    **Responsibility (narrow):**
      * Map raw plotly event types to normalized ``kind`` strings.
      * Extract ``selection`` (from click/hover/selected points) and
        ``viewport`` (from relayout/restyle axis ranges) out of the
        raw ``plotly_event`` payload.

    Does **not** update component parameters or invoke FigureWidget
    callbacks — those live in the Plotly pane itself.
    """

    _event_names: tuple[str, ...] = ('plotly_event',)

    _KIND_MAP: dict[str, str] = {
        'click': 'point_click',
        'doubleclick': 'point_doubleclick',
        'clickannotation': 'annotation_click',
        'hover': 'point_hover',
        'unhover': 'point_leave',
        'selected': 'selection',
        'deselect': 'deselect',
        'relayout': 'viewport_change',
        'restyle': 'style_change',
    }

    def __init__(
        self,
        component: Plotly,
        source_id: str | None = None,
        dataset_id: str | None = None,
        store=None,
    ) -> None:
        super().__init__(component, source_id, dataset_id, store)

    def get_kind(self, event: AdapterEvent) -> str:
        raw = event.raw
        etype = (
            getattr(raw, 'data', {}).get('type', event.event_name)
            if hasattr(raw, 'data')
            else event.event_name
        )
        return self._KIND_MAP.get(etype, etype)

    def _extract_points(self, data: dict[str, t.Any]) -> dict[str, t.Any] | None:
        if not data or 'points' not in data:
            return None
        points = data['points'] or []
        if not points:
            return None

        has_nested = all('pointNumbers' in (p or {}) for p in points)

        trace_indexes: list[int] = []
        point_indexes: list[int] = []
        xs: list[t.Any] = []
        ys: list[t.Any] = []
        zs: list[t.Any] = []
        has_z = any((p or {}).get('z') is not None for p in points)

        if has_nested:
            for point_obj in points:
                if not point_obj:
                    continue
                for i in range(len(point_obj.get('pointNumbers', []))):
                    point_indexes.append(point_obj['pointNumbers'][i])
                    xs.append(point_obj.get('x'))
                    ys.append(point_obj.get('y'))
                    trace_indexes.append(point_obj.get('curveNumber', 0))
                    if has_z and 'z' in point_obj:
                        zs.append(point_obj.get('z'))
        else:
            for point_obj in points:
                if not point_obj:
                    continue
                trace_indexes.append(point_obj.get('curveNumber', 0))
                point_indexes.append(point_obj.get('pointNumber', 0))
                xs.append(point_obj.get('x'))
                ys.append(point_obj.get('y'))
                if has_z and 'z' in point_obj:
                    zs.append(point_obj.get('z'))

        selection: dict[str, t.Any] = {
            'trace_indexes': trace_indexes,
            'point_indexes': point_indexes,
            'indexes': point_indexes,
            'xs': xs,
            'ys': ys,
        }
        if has_z:
            selection['zs'] = zs
        if data.get('selector'):
            selection['selector'] = data['selector']
        if data.get('device_state'):
            selection['device_state'] = data['device_state']
        return selection

    def _extract_viewport(self, data: dict[str, t.Any] | None) -> dict[str, t.Any] | None:
        if not isinstance(data, dict):
            return None
        import re
        range_re = re.compile(r'^(.+)\.range\[(\d+)\]$')
        viewport: dict[str, t.Any] = {}
        collected: dict[str, list[t.Any]] = {}
        for k, v in data.items():
            m = range_re.match(k)
            if m:
                axis = m.group(1)
                idx = int(m.group(2))
                collected.setdefault(axis, [None, None])[idx] = v
        for axis, bounds in collected.items():
            if bounds[0] is not None and bounds[1] is not None:
                viewport[axis] = bounds
        return viewport or None

    def extract_payload(self, event: AdapterEvent) -> dict[str, t.Any]:
        raw = event.raw
        if hasattr(raw, 'data'):
            etype = raw.data.get('type', '')
            data = raw.data.get('data')
        else:
            etype = ''
            data = raw

        result: dict[str, t.Any] = {'event_type': etype, 'raw_data': data}

        if etype in ('click', 'doubleclick', 'hover', 'selected') and data is not None:
            selection = self._extract_points(data)
            if selection:
                result['selection'] = selection

        if etype in ('relayout', 'restyle'):
            viewport = self._extract_viewport(data)
            if viewport:
                result['viewport'] = viewport

        return result
