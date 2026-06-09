"""
Regression tests for the unified interaction event adapter.

Covers:
  1. Public consumer API: interaction_store, subscribe_interaction, unsubscribe_interaction
     on Plotly, Vega, ECharts, Tabulator components.
  2. Per-kind subscriptions and unsubscribe correctness.
  3. Store isolation between component instances.
  4. Legacy _process_event side-effect preservation:
       - Plotly: {etype}_data parameter updates (click / hover / select / relayout / restyle / deselect)
       - Vega:   selection parameter writes (point + interval)
       - ECharts: on_event / js_on_event callback dispatch (global + query-scoped)
       - Tabulator: selection-change, cell-click, table-edit callbacks with value resolution
"""
from __future__ import annotations

from importlib.util import find_spec

import pytest

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Optional deps
# ---------------------------------------------------------------------------
def _has(name: str) -> bool:
    return find_spec(name) is not None


plotly_available = pytest.mark.skipif(not _has("plotly"), reason="requires plotly")
vega_available = pytest.mark.skipif(not _has("altair"), reason="requires altair/vega")
echarts_available = pytest.mark.skipif(not _has("pyecharts"), reason="requires pyecharts")


# ---------------------------------------------------------------------------
# Lightweight fake Bokeh event objects (mirroring what TS sends to _process_event)
# ---------------------------------------------------------------------------
class _FakeBase:
    event_name: str = ""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# 1. Consumer API exposure
# ---------------------------------------------------------------------------
@plotly_available
def test_plotly_exposes_interaction_apis():
    import plotly.graph_objs as go
    from panel.pane.plotly import Plotly

    trace = go.Scatter(x=[0, 1], y=[2, 3])
    p = Plotly(go.Figure([trace]))

    assert hasattr(p, "interaction_store")
    assert hasattr(p, "subscribe_interaction")
    assert hasattr(p, "unsubscribe_interaction")
    assert callable(p.subscribe_interaction)
    assert callable(p.unsubscribe_interaction)


@vega_available
def test_vega_exposes_interaction_apis():
    import altair as alt
    from panel.pane.vega import Vega

    df = pd.DataFrame({"x": [1, 2, 3], "y": [10, 20, 30]})
    chart = alt.Chart(df).mark_point().encode(x="x", y="y")
    v = Vega(chart)

    assert hasattr(v, "interaction_store")
    assert hasattr(v, "subscribe_interaction")
    assert hasattr(v, "unsubscribe_interaction")


@echarts_available
def test_echarts_exposes_interaction_apis():
    from pyecharts.charts import Bar
    from panel.pane.echarts import ECharts

    bar = Bar().add_xaxis(["A", "B"]).add_yaxis("s", [1, 2])
    e = ECharts(bar)

    assert hasattr(e, "interaction_store")
    assert hasattr(e, "subscribe_interaction")
    assert hasattr(e, "unsubscribe_interaction")


def test_tabulator_exposes_interaction_apis():
    from panel.widgets.tables import Tabulator

    df = pd.DataFrame({"a": [1, 2, 3], "b": [100, 200, 300]})
    t = Tabulator(df)

    assert hasattr(t, "interaction_store")
    assert hasattr(t, "subscribe_interaction")
    assert hasattr(t, "unsubscribe_interaction")


# ---------------------------------------------------------------------------
# 2. Plotly regression — legacy {etype}_data preserved + StandardEvent published
# ---------------------------------------------------------------------------
@plotly_available
def test_plotly_etype_data_and_standard_event():
    import plotly.graph_objs as go
    from panel.pane.plotly import Plotly

    trace = go.Scatter(x=list(range(10)), y=list(range(10)), customdata=[f"c{i}" for i in range(10)])
    p = Plotly(go.Figure([trace]))

    # Plotly _process_event uses event.data['type'] and event.data['data']
    class PlotlyEv(_FakeBase):
        event_name = "plotly_event"

    # click
    ev = PlotlyEv(data={"type": "click", "data": {"points": [{"curveNumber": 0, "pointNumber": 3, "pointIndex": 3, "x": 3, "y": 3, "customdata": "c3"}]}})
    p._process_event(ev)
    assert p.click_data is not None
    assert p.click_data["points"][0]["pointIndex"] == 3

    # hover
    ev2 = PlotlyEv(data={"type": "hover", "data": {"points": [{"curveNumber": 0, "pointNumber": 7, "x": 7, "y": 7}]}})
    p._process_event(ev2)
    assert p.hover_data is not None
    assert p.hover_data["points"][0]["pointNumber"] == 7

    # selected
    ev3 = PlotlyEv(data={"type": "selected", "data": {"points": [
        {"curveNumber": 0, "pointNumber": 1, "x": 1, "y": 1},
        {"curveNumber": 0, "pointNumber": 4, "x": 4, "y": 4},
    ]}})
    p._process_event(ev3)
    assert p.selected_data is not None
    assert len(p.selected_data["points"]) == 2

    # relayout
    ev4 = PlotlyEv(data={"type": "relayout", "data": {"xaxis.range[0]": 0, "xaxis.range[1]": 100, "yaxis.range[0]": -50, "yaxis.range[1]": 50}})
    p._process_event(ev4)
    assert p.relayout_data is not None
    assert p.relayout_data["xaxis.range[0]"] == 0

    # restyle
    ev5 = PlotlyEv(data={"type": "restyle", "data": [{"marker.color": ["red"]}, [0]]})
    p._process_event(ev5)
    assert p.restyle_data is not None

    # deselect → TS sends type="selected" with data=None → selected_data becomes None
    ev6 = PlotlyEv(data={"type": "selected", "data": None})
    p._process_event(ev6)
    assert p.selected_data is None

    # StandardEvent published
    last = p.interaction_store.get_last("point_click")
    assert last is not None
    assert last.kind == "point_click"
    assert last.selection is not None
    assert 3 in last.selection["indexes"]
    assert last.source_id == p.name

    vp = p.interaction_store.get_last("viewport_change")
    assert vp is not None
    assert vp.viewport is not None
    assert "xaxis" in vp.viewport
    assert vp.viewport["xaxis"] == [0, 100]
    assert vp.viewport["yaxis"] == [-50, 50]


# ---------------------------------------------------------------------------
# 3. Vega regression — selection writes preserved + StandardEvent published
# ---------------------------------------------------------------------------
@vega_available
def test_vega_selection_writes_and_standard_event():
    import altair as alt
    from panel.pane.vega import Vega

    df = pd.DataFrame({"x": list(range(50)), "y": list(range(50))})

    # Define named selections so Vega registers them as Selection params
    my_pt = alt.selection_point(name="my_pt")
    my_brush = alt.selection_interval(name="my_brush")
    chart = (
        alt.Chart(df)
        .mark_point()
        .encode(x="x", y="y")
        .add_params(my_pt, my_brush)
    )
    v = Vega(chart)

    # Confirm selections were picked up
    assert "my_pt" in v._selections
    assert "my_brush" in v._selections

    # Vega _process_event uses event.data['type'] (selection name) and event.data['value']
    class VegaEv(_FakeBase):
        event_name = "vega_event"

    # point selection (has _vgsid_ list)
    pt_ev = VegaEv(data={"type": "my_pt", "value": [{"_vgsid_": 2}, {"_vgsid_": 6}, {"_vgsid_": 31}]})
    v._process_event(pt_ev)
    assert v.selection.my_pt == [{"_vgsid_": 2}, {"_vgsid_": 6}, {"_vgsid_": 31}]

    # interval selection (has coordinates)
    iv_ev = VegaEv(data={"type": "my_brush", "value": {"x": [10, 30], "y": [5, 25]}})
    v._process_event(iv_ev)
    assert v.selection.my_brush["x"] == [10, 30]

    # StandardEvent for the last point selection
    last = v.interaction_store.get_last("point_selection")
    assert last is not None
    # _vgsid_ is the raw 1-based Vega row id, passed through as-is
    assert sorted(last.selection["indexes"]) == [2, 6, 31]


# ---------------------------------------------------------------------------
# 4. ECharts regression — on_event callbacks preserved + StandardEvent published
# ---------------------------------------------------------------------------
@echarts_available
def test_echarts_on_event_callbacks_and_standard_event():
    from pyecharts.charts import Bar
    from panel.pane.echarts import ECharts

    bar = Bar().add_xaxis(["A", "B", "C"]).add_yaxis("s", [1, 2, 3])
    e = ECharts(bar)

    fired_global = []
    fired_query = []
    fired_legend = []
    e.on_event("click", lambda obj: fired_global.append(obj))
    e.on_event("click", lambda obj: fired_query.append(obj), query="s")
    e.on_event("legendselectchanged", lambda obj: fired_legend.append(obj))

    # ECharts _process_event uses event.type, event.query, event.data (direct attributes)
    class EChartsEv(_FakeBase):
        event_name = "echarts_event"

    ev = EChartsEv(type="click", query="s", data={"dataIndex": 1, "name": "B", "value": 2})
    e._process_event(ev)

    assert len(fired_global) == 1
    assert fired_global[0]["dataIndex"] == 1
    assert len(fired_query) == 1
    assert fired_query[0]["dataIndex"] == 1

    ev2 = EChartsEv(type="legendselectchanged", query=None, data={"selected": {"s": False, "t": True}})
    e._process_event(ev2)
    assert len(fired_legend) == 1
    assert fired_legend[0]["selected"]["s"] is False

    # StandardEvent
    last = e.interaction_store.get_last("point_click")
    assert last is not None
    assert last.kind == "point_click"
    assert last.selection["indexes"] == [1]


# ---------------------------------------------------------------------------
# 5. Tabulator regression — callbacks + value resolution + StandardEvent
# ---------------------------------------------------------------------------
def test_tabulator_selection_click_edit_and_standard_event():
    from panel.widgets.tables import Tabulator

    df = pd.DataFrame({"a": [1, 2, 3], "b": [100, 200, 300]}, index=["r0", "r1", "r2"])
    t = Tabulator(df)

    # selection-change — SelectionEvent has event_name, indices, selected, flush
    class SelEv(_FakeBase):
        event_name = "selection-change"
    sel_ev = SelEv(indices=[0, 2], selected=True, flush=True)
    t._process_event(sel_ev)

    sel_std = t.interaction_store.get_last("selection")
    assert sel_std is not None
    assert sel_std.selection["indexes"] == [0, 2]
    assert sel_std.selection["type"] == "row"

    # cell-click with value resolution
    click_log = []
    t.on_click(lambda ev: click_log.append((ev.row, ev.column, ev.value)))

    class ClickEv(_FakeBase):
        event_name = "cell-click"
    cl_ev = ClickEv(row=1, column="a", value=None)
    t._process_event(cl_ev)
    assert click_log[-1] == (1, "a", 2)

    cl_std = t.interaction_store.get_last("cell_click")
    assert cl_std is not None
    assert cl_std.selection["indexes"] == [1]
    assert cl_std.selection["field"] == "a"
    assert cl_std.payload["value"] == 2

    # table-edit (post-edit)
    edit_log = []
    t.on_edit(lambda ev: edit_log.append((ev.row, ev.column, ev.old, ev.value)))

    class EditEv(_FakeBase):
        event_name = "table-edit"
    ed_ev = EditEv(row=2, column="b", value=9999, old=None, pre=False)
    t._process_event(ed_ev)
    assert edit_log[-1][:2] == (2, "b")
    assert edit_log[-1][2] is None
    assert int(edit_log[-1][3]) == 300

    ed_std = t.interaction_store.get_last("cell_edit")
    assert ed_std is not None
    assert ed_std.selection["indexes"] == [2]
    assert ed_std.selection["field"] == "b"


# ---------------------------------------------------------------------------
# 6. subscribe_interaction: kind filter + unsubscribe
# ---------------------------------------------------------------------------
@plotly_available
def test_subscribe_and_unsubscribe_interaction():
    import plotly.graph_objs as go
    from panel.pane.plotly import Plotly

    trace = go.Scatter(x=list(range(10)), y=list(range(10)))
    p = Plotly(go.Figure([trace]))

    all_events = []
    click_only = []
    cb_all = lambda e: all_events.append(e.kind)
    cb_click = lambda e: click_only.append(e.kind)

    p.subscribe_interaction(cb_all)
    p.subscribe_interaction(cb_click, kind="point_click")

    class PlotlyEv(_FakeBase):
        event_name = "plotly_event"

    ev1 = PlotlyEv(data={"type": "click", "data": {"points": [{"curveNumber": 0, "pointNumber": 3, "x": 3, "y": 3}]}})
    ev2 = PlotlyEv(data={"type": "relayout", "data": {"xaxis.range[0]": 0, "xaxis.range[1]": 1}})
    p._process_event(ev1)
    p._process_event(ev2)

    assert all_events == ["point_click", "viewport_change"]
    assert click_only == ["point_click"]

    # unsubscribe cb_click (kind-scoped) — cb_all still fires
    p.unsubscribe_interaction(cb_click, kind="point_click")
    p._process_event(ev1)
    assert len(click_only) == 1  # kind-filtered callback did not fire
    assert len(all_events) == 3  # global callback still fired

    # unsubscribe cb_all (global) — no subscribers left
    p.unsubscribe_interaction(cb_all)
    p._process_event(ev2)
    assert len(all_events) == 3  # no callback fired
    assert len(click_only) == 1


# ---------------------------------------------------------------------------
# 7. Store isolation between component instances
# ---------------------------------------------------------------------------
@plotly_available
def test_store_isolated_between_components():
    import plotly.graph_objs as go
    from panel.pane.plotly import Plotly

    trace = go.Scatter(x=[0, 1], y=[2, 3])
    p1 = Plotly(go.Figure([trace]))
    p2 = Plotly(go.Figure([trace]))
    assert p1.interaction_store is not p2.interaction_store

    class PlotlyEv(_FakeBase):
        event_name = "plotly_event"

    ev1 = PlotlyEv(data={"type": "click", "data": {"points": [{"curveNumber": 0, "pointNumber": 0, "x": 0, "y": 2}]}})
    p1._process_event(ev1)

    assert p1.interaction_store.get_last("point_click") is not None
    assert p2.interaction_store.get_last("point_click") is None
