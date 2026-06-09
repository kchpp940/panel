from __future__ import annotations

import typing as t

from .base import AdapterEvent, InteractionAdapter

if t.TYPE_CHECKING:
    from ..pane.vega import Vega


class VegaAdapter(InteractionAdapter):
    """
    Interaction adapter for Vega/Vega-Lite panes.

    **Responsibility (narrow):**
      * Detect whether the named selection is a point or interval
        selection and report the normalized ``kind``.
      * Extract a normalized ``selection`` dict from the raw vega
        signal value (point indexes from ``_vgsid_`` or interval
        min/max bounds).

    Does **not** touch the component's ``selection`` Parameterized
    object — that is the component's own concern.
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
        name = (
            raw.data.get('type', event.event_name)
            if hasattr(raw, 'data')
            else event.event_name
        )
        selection_types = getattr(self._component, '_selections', {})
        stype = selection_types.get(name, 'point')
        return 'interval_selection' if stype == 'interval' else 'point_selection'

    def _extract_selection(self, name: str, value: t.Any) -> dict[str, t.Any] | None:
        if value is None:
            return None
        selection_types = getattr(self._component, '_selections', {})
        stype = selection_types.get(name, 'point')

        if stype == 'interval':
            if isinstance(value, dict):
                for field, bounds in value.items():
                    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                        return {
                            'type': 'interval',
                            'field': field,
                            'min': bounds[0],
                            'max': bounds[1],
                            'values': list(bounds),
                        }
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
