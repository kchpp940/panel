from bokeh.models import Div

from panel.layout.gridstack import GridStack


def _make_gridstack_and_items():
    div1 = Div()
    div2 = Div()
    gspec = GridStack(width=800, height=600, ncols=4, nrows=3)
    gspec[0, 0] = div1
    gspec[1, 1] = div2
    pane1 = gspec.objects[(0, 0, 1, 1)]
    pane2 = gspec.objects[(1, 1, 2, 2)]
    return gspec, pane1, pane2


def test_gridstack_state_raw_user_source_writes_object_sizing():
    gspec, pane1, pane2 = _make_gridstack_and_items()

    item1_id = str(id(pane1))
    item2_id = str(id(pane2))

    gspec._state_raw = {
        "source": "user",
        "items": [
            {"id": item1_id, "x0": 0, "y0": 0, "x1": 2, "y1": 2},
            {"id": item2_id, "x0": 2, "y0": 1, "x1": 4, "y1": 3},
        ],
    }

    assert (0, 0, 2, 2) in gspec.objects
    assert gspec.objects[(0, 0, 2, 2)] is pane1
    assert (1, 2, 3, 4) in gspec.objects
    assert gspec.objects[(1, 2, 3, 4)] is pane2

    assert pane1.width == 400
    assert pane1.height == 400
    assert pane2.width == 400
    assert pane2.height == 400

    expected_state = [
        {"id": item1_id, "x0": 0, "y0": 0, "x1": 2, "y1": 2},
        {"id": item2_id, "x0": 2, "y0": 1, "x1": 4, "y1": 3},
    ]
    assert gspec.state == expected_state


def test_gridstack_state_raw_layout_source_does_not_write_sizing():
    gspec, pane1, pane2 = _make_gridstack_and_items()

    original_width_1, original_height_1 = pane1.width, pane1.height
    original_width_2, original_height_2 = pane2.width, pane2.height

    item1_id = str(id(pane1))
    item2_id = str(id(pane2))

    gspec._state_raw = {
        "source": "layout",
        "items": [
            {"id": item1_id, "x0": 0, "y0": 0, "x1": 2, "y1": 2},
            {"id": item2_id, "x0": 2, "y0": 1, "x1": 4, "y1": 3},
        ],
    }

    assert (0, 0, 2, 2) in gspec.objects
    assert (1, 2, 3, 4) in gspec.objects

    assert pane1.width == original_width_1
    assert pane1.height == original_height_1
    assert pane2.width == original_width_2
    assert pane2.height == original_height_2


def test_gridstack_state_raw_default_source_layout():
    gspec, pane1, pane2 = _make_gridstack_and_items()

    original_width_1, original_height_1 = pane1.width, pane1.height
    item1_id = str(id(pane1))
    item2_id = str(id(pane2))

    gspec._state_raw = {
        "items": [
            {"id": item1_id, "x0": 0, "y0": 0, "x1": 2, "y1": 2},
            {"id": item2_id, "x0": 2, "y0": 1, "x1": 4, "y1": 3},
        ],
    }

    assert pane1.width == original_width_1
    assert pane1.height == original_height_1


def test_gridstack_stretch_both_never_sets_fixed_sizing():
    div1 = Div()
    div2 = Div(sizing_mode='stretch_both')
    gspec = GridStack(sizing_mode='stretch_both', ncols=4, nrows=3)
    gspec[0, 0] = div1
    gspec[0, 1] = div2

    pane1 = gspec.objects[(0, 0, 1, 1)]
    pane2 = gspec.objects[(0, 1, 1, 2)]

    item1_id = str(id(pane1))
    item2_id = str(id(pane2))

    gspec._state_raw = {
        "source": "user",
        "items": [
            {"id": item1_id, "x0": 0, "y0": 0, "x1": 2, "y1": 2},
            {"id": item2_id, "x0": 2, "y0": 0, "x1": 4, "y1": 3},
        ],
    }

    assert pane1.sizing_mode == 'stretch_both'
    assert pane1.width is None
    assert pane1.height is None

    assert pane2.sizing_mode == 'stretch_both'
    assert pane2.width is None
    assert pane2.height is None


def test_gridstack_fixed_recompute_sizing_on_ncols_change():
    div1 = Div()
    gspec = GridStack(width=800, height=600, ncols=4, nrows=3)
    gspec[0, 0:2] = div1

    pane1 = gspec.objects[(0, 0, 1, 2)]

    assert pane1.width == 400
    assert pane1.height == 200

    gspec.ncols = 8
    assert pane1.width == 200

    gspec.width = 1600
    assert pane1.width == 400


def test_gridstack_stretch_does_not_recompute_sizing_on_ncols_change():
    div1 = Div()
    gspec = GridStack(sizing_mode='stretch_both', ncols=4, nrows=3)
    gspec[0, 0:2] = div1

    pane1 = gspec.objects[(0, 0, 1, 2)]

    assert pane1.sizing_mode == 'stretch_both'
    original_width, original_height = pane1.width, pane1.height

    gspec.ncols = 8

    assert pane1.sizing_mode == 'stretch_both'
    assert pane1.width == original_width
    assert pane1.height == original_height


def test_gridstack_objects_synced_to_state_public_api():
    gspec, pane1, pane2 = _make_gridstack_and_items()

    item1_id = str(id(pane1))
    item2_id = str(id(pane2))

    new_items = [
        {"id": item1_id, "x0": 1, "y0": 0, "x1": 3, "y1": 1},
        {"id": item2_id, "x0": 0, "y0": 2, "x1": 4, "y1": 3},
    ]
    gspec._state_raw = {"source": "layout", "items": new_items}

    assert gspec.state == new_items
    assert (0, 1, 1, 3) in gspec.objects
    assert (2, 0, 3, 4) in gspec.objects


def test_gridstack_updating_objects_flag_prevents_recursion():
    gspec, pane1, _ = _make_gridstack_and_items()
    item1_id = str(id(pane1))

    gspec._updating_objects = True
    try:
        gspec._state_raw = {
            "source": "user",
            "items": [{"id": item1_id, "x0": 0, "y0": 0, "x1": 3, "y1": 3}],
        }
    finally:
        gspec._updating_objects = False

    assert (0, 0, 1, 1) in gspec.objects or (0, 0, 3, 3) in gspec.objects
