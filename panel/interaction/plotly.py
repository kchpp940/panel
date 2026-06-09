from __future__ import annotations

import typing as t

from .base import AdapterEvent, InteractionAdapter, StandardEvent

if t.TYPE_CHECKING:
    from ..pane.plotly import Plotly


class PlotlyAdapter(InteractionAdapter):
    """
    Interaction adapter for Plotly panes.

    **Owns the entire plotly_event handling flow:**
      * Updates ``{event}_data`` on the component
      * Extracts normalized ``selection`` / ``viewport`` / ``payload``
      * Fires the legacy ``FigureWidget._handler_js2py_pointsCallback``
        when the wrapped object is a FigureWidget
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
        etype = getattr(raw, 'data', {}).get('type', event.event_name) if hasattr(raw, 'data') else event.event_name
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

        selector = data.get('selector')
        if selector:
            selection['selector'] = selector
        device_state = data.get('device_state')
        if device_state:
            selection['device_state'] = device_state

        return selection

    def _extract_points_object(self, data: dict[str, t.Any]) -> dict[str, t.Any] | None:
        """
        Build the legacy points_object dict consumed by
        FigureWidget._handler_js2py_pointsCallback.
        """
        if not data or 'points' not in data:
            return None
        points = data['points']
        if not points:
            return None

        has_nested = all('pointNumbers' in (p or {}) for p in points)
        has_z = points[0] is not None and 'z' in points[0]

        points_object: dict[str, t.Any] = {
            'trace_indexes': [],
            'point_indexes': [],
            'xs': [],
            'ys': [],
        }
        if has_z:
            points_object['zs'] = []

        num_point_numbers = 0
        if has_nested:
            for point_obj in points:
                n = len(point_obj.get('pointNumbers', []))
                num_point_numbers += n
                for i in range(n):
                    points_object['point_indexes'].append(point_obj['pointNumbers'][i])
                    points_object['xs'].append(point_obj.get('x'))
                    points_object['ys'].append(point_obj.get('y'))
                    points_object['trace_indexes'].append(point_obj['curveNumber'])
                    if has_z and 'z' in point_obj:
                        points_object['zs'].append(point_obj.get('z'))
            single_trace = True
            for i in range(1, num_point_numbers):
                if points_object['trace_indexes'][i - 1] != points_object['trace_indexes'][i]:
                    single_trace = False
                    break
            if single_trace:
                points_object['point_indexes'].sort()
        else:
            for point_obj in points:
                points_object['trace_indexes'].append(point_obj['curveNumber'])
                points_object['point_indexes'].append(point_obj['pointNumber'])
                points_object['xs'].append(point_obj.get('x'))
                points_object['ys'].append(point_obj.get('y'))
                if has_z and 'z' in point_obj:
                    points_object['zs'].append(point_obj.get('z'))

        return points_object

    def _extract_viewport(self, data: dict[str, t.Any] | None) -> dict[str, t.Any] | None:
        if not data:
            return None
        if isinstance(data, dict):
            viewport = {}
            for k, v in data.items():
                if k.endswith('.range'):
                    axis_name = k.rsplit('.', 1)[0]
                    viewport[axis_name] = v
            if viewport:
                return viewport
        return None

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
            points_object = self._extract_points_object(data)
            if points_object:
                result['_points_object'] = points_object

        if etype in ('relayout', 'restyle'):
            viewport = self._extract_viewport(data)
            if viewport:
                result['viewport'] = viewport

        return result

    def on_standardized_event(self, event: StandardEvent, raw: AdapterEvent) -> None:
        comp = self._component
        etype = event.payload.get('event_type', '')
        data = event.payload.get('raw_data')

        if etype:
            pname = f'{etype}_data'
            if hasattr(comp, pname):
                if getattr(comp, pname) == data:
                    comp.param.trigger(pname)
                else:
                    comp.param.update(**{pname: data})

        if data is None or not hasattr(comp.object, '_handler_js2py_pointsCallback'):
            return

        points_object = event.payload.get('_points_object')
        if not points_object:
            return

        comp._figure._handler_js2py_pointsCallback(
            {
                'new': dict(
                    event_type=f'plotly_{etype}',
                    points=points_object,
                    selector=(event.selection or {}).get('selector'),
                    device_state=(event.selection or {}).get('device_state'),
                )
            }
        )

    def register_events(self, model, doc, comm=None) -> None:
        self._component._register_events('plotly_event', model=model, doc=doc, comm=comm)
