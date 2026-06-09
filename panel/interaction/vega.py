from __future__ import annotations

import typing as t

from .base import AdapterEvent, InteractionAdapter, StandardEvent

if t.TYPE_CHECKING:
    from ..pane.vega import Vega


class VegaAdapter(InteractionAdapter):
    """
    Interaction adapter for Vega/Vega-Lite panes.

    **Owns the entire vega_event handling flow:**
      * Normalizes interval vs point selections
      * Updates the component's ``selection`` Parameterized object
    """

    _event_names: tuple[str, ...] = ('vega_event',)

    def __init__(
        self,
        component: Vega,
        source_id: str | None = None,
        dataset_id: str | None = None,
        store=None,
    ) -> None:
        super().__init__(component, source_id, dataset_id, store)

    def get_kind(self, event: AdapterEvent) -> str:
        raw = event.raw
        if hasattr(raw, 'data'):
            name = raw.data.get('type', event.event_name)
        else:
            name = event.event_name
        selection_types = getattr(self._component, '_selections', {})
        stype = selection_types.get(name, 'point')
        return 'interval_selection' if stype == 'interval' else 'point_selection'

    def _extract_selection(
        self, name: str, value: t.Any
    ) -> dict[str, t.Any] | None:
        if value is None:
            return None
        selection_types = getattr(self._component, '_selections', {})
        stype = selection_types.get(name, 'point')

        if stype == 'interval':
            if isinstance(value, dict):
                selection: dict[str, t.Any] = {'type': 'interval'}
                for field, bounds in value.items():
                    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                        selection['field'] = field
                        selection['min'] = bounds[0]
                        selection['max'] = bounds[1]
                        selection['values'] = list(bounds)
                return selection
            return {'type': 'interval', 'value': value}

        if isinstance(value, list):
            point_indexes: list[int] = []
            records: list[dict[str, t.Any]] = []
            for v in value:
                if isinstance(v, dict):
                    if '_vgsid_' in v:
                        point_indexes.append(int(v['_vgsid_']))
                    else:
                        for k, val in v.items():
                            records.append({k: val})
                elif isinstance(v, int):
                    point_indexes.append(v)
            selection = {'type': 'point', 'indexes': point_indexes}
            if records:
                selection['records'] = records
            return selection

        return {'type': 'point', 'value': value}

    def extract_payload(self, event: AdapterEvent) -> dict[str, t.Any]:
        raw = event.raw
        if hasattr(raw, 'data'):
            name = raw.data.get('type', '')
            value = raw.data.get('value')
        else:
            name = ''
            value = raw

        result: dict[str, t.Any] = {'selection_name': name, 'raw_value': value}
        selection = self._extract_selection(name, value)
        if selection:
            result['selection'] = selection
        return result

    def on_standardized_event(self, event: StandardEvent, raw: AdapterEvent) -> None:
        comp = self._component
        name = event.payload.get('selection_name', '')
        value = event.payload.get('raw_value')
        if not name:
            return
        if not hasattr(comp.selection.param, name):
            return
        stype = getattr(comp, '_selections', {}).get(name)
        if stype != 'interval' and isinstance(value, (list, tuple)):
            value = list(value)
        try:
            comp.selection.param.update(**{name: value})
        except Exception:
            pass

    def register_events(self, model, doc, comm=None) -> None:
        self._component._register_events('vega_event', model=model, doc=doc, comm=comm)
