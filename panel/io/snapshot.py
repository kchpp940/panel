"""
Dashboard state snapshot functionality for saving and restoring
widget values, layout states, location parameters, and pane states.
"""
from __future__ import annotations

import base64
import hashlib
import json
import typing as t
import zlib

from collections import defaultdict
from contextlib import suppress
from weakref import WeakKeyDictionary

import param

from bokeh.document import Document

from ..util import edit_readonly
from .cache import is_equal

if t.TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..layout import Accordion, Tabs
    from ..pane.base import PaneBase
    from ..template.base import BaseTemplate
    from ..viewable import Viewable
    from ..widgets.base import Widget
    from .location import Location
    from .state import state as _state_type

    T = t.TypeVar("T")


SNAPSHOT_QUERY_PARAM = "_snapshot"
SNAPSHOT_BASE_URL = "_snapshots"


def _get_state():
    from .state import state
    return state


def _get_location_type():
    from .location import Location
    return Location


class _SnapshotStore:
    """
    Per-session and per-document snapshot storage.
    """

    _snapshots: t.ClassVar[WeakKeyDictionary[Document, dict[str, dict[str, t.Any]]]] = (
        WeakKeyDictionary()
    )

    _global_snapshots: t.ClassVar[dict[str, dict[str, t.Any]]] = {}

    @classmethod
    def get_snapshots(cls, doc: Document | None = None) -> dict[str, dict[str, t.Any]]:
        state = _get_state()
        doc = doc or state.curdoc
        if doc is None:
            return cls._global_snapshots
        if doc not in cls._snapshots:
            cls._snapshots[doc] = {}
        return cls._snapshots[doc]

    @classmethod
    def save_snapshot(
        cls, name: str, snapshot_data: dict[str, t.Any],
        doc: Document | None = None
    ) -> None:
        snapshots = cls.get_snapshots(doc)
        snapshots[name] = snapshot_data

    @classmethod
    def load_snapshot(
        cls, name: str, doc: Document | None = None
    ) -> dict[str, t.Any] | None:
        snapshots = cls.get_snapshots(doc)
        return snapshots.get(name)

    @classmethod
    def delete_snapshot(cls, name: str, doc: Document | None = None) -> bool:
        snapshots = cls.get_snapshots(doc)
        if name in snapshots:
            del snapshots[name]
            return True
        return False

    @classmethod
    def list_snapshots(cls, doc: Document | None = None) -> list[str]:
        snapshots = cls.get_snapshots(doc)
        return sorted(snapshots.keys())


def _collect_viewables(root: Viewable | BaseTemplate) -> list[Viewable]:
    """
    Recursively collect all Viewable objects in the component tree.
    """
    from ..template.base import BaseTemplate
    from ..viewable import Viewable

    result: list[Viewable] = []
    seen: set[int] = set()

    def _visit(obj: t.Any) -> None:
        if id(obj) in seen:
            return
        seen.add(id(obj))

        if not isinstance(obj, Viewable):
            return

        result.append(obj)

        with suppress(Exception):
            children = obj.select()
            for child in children:
                if child is not obj:
                    _visit(child)

        if isinstance(obj, BaseTemplate):
            for attr in ("main", "sidebar", "header", "modal"):
                container = getattr(obj, attr, None)
                if container is not None:
                    _visit(container)

        if hasattr(obj, "objects"):
            for item in obj.objects:
                _visit(item)

    if isinstance(root, BaseTemplate):
        _visit(root)
        for attr in ("main", "sidebar", "header", "modal"):
            container = getattr(root, attr, None)
            if container is not None:
                _visit(container)
    else:
        _visit(root)

    return list({id(v): v for v in result}.values())


def _get_viewable_path(obj: Viewable, root: Viewable | BaseTemplate) -> str | None:
    """
    Generate a stable path identifier for a Viewable within the tree.
    Uses name, type, and positional information.
    """
    from ..template.base import BaseTemplate

    parts: list[str] = []
    name = getattr(obj, "name", "") or getattr(obj, "label", "")
    if name:
        parts.append(f"name:{name}")

    type_name = type(obj).__name__
    parts.append(f"type:{type_name}")

    def _find_path(node: t.Any, path: list[str]) -> bool:
        if node is obj:
            parts.extend(path)
            return True

        if hasattr(node, "objects"):
            for i, item in enumerate(node.objects):
                new_path = path + [f"idx:{i}"]
                if _find_path(item, new_path):
                    return True

        if isinstance(node, BaseTemplate):
            for attr in ("main", "sidebar", "header", "modal"):
                container = getattr(node, attr, None)
                if container is not None:
                    new_path = path + [f"slot:{attr}"]
                    if _find_path(container, new_path):
                        return True

        if hasattr(node, "select"):
            try:
                children = node.select()
            except Exception:
                children = []
            for i, child in enumerate(children):
                if child is node:
                    continue
                new_path = path + [f"child:{i}"]
                if _find_path(child, new_path):
                    return True

        return False

    try:
        _find_path(root, [])
    except Exception:
        pass

    path_str = "|".join(parts)
    if len(path_str) > 200:
        path_str = hashlib.sha256(path_str.encode()).hexdigest()[:32]
    return path_str


def _serialize_value(obj: t.Any, param_name: str, value: t.Any) -> t.Any:
    """
    Serialize a parameter value safely.
    """
    if value is None:
        return None

    try:
        if hasattr(obj.param[param_name], "serialize"):
            serialized = obj.param[param_name].serialize(value)
            json.dumps(serialized)
            return {"__serialized__": True, "value": serialized}
    except Exception:
        pass

    try:
        json.dumps(value)
        return value
    except Exception:
        pass

    return None


def _deserialize_value(
    obj: t.Any, param_name: str, stored: t.Any
) -> t.Any:
    """
    Deserialize a stored parameter value.
    """
    if stored is None:
        return None

    if isinstance(stored, dict) and stored.get("__serialized__"):
        try:
            if hasattr(obj.param[param_name], "deserialize"):
                return obj.param[param_name].deserialize(stored["value"])
        except Exception:
            return None
        return stored["value"]

    try:
        if hasattr(obj.param[param_name], "deserialize_value"):
            return obj.param[param_name].deserialize_value(stored)
    except Exception:
        pass

    return stored


_WIDGET_VALUE_PARAMS: t.ClassVar[dict[type, list[str]]] = defaultdict(lambda: ["value"])


def _get_widget_params(widget: Widget) -> list[str]:
    """
    Get list of important parameters to save for a widget.
    """
    from ..widgets.base import Widget
    widget_type = type(widget)
    if widget_type in _WIDGET_VALUE_PARAMS:
        return _WIDGET_VALUE_PARAMS[widget_type]

    params = ["value"]
    for pname in ("disabled", "visible", "options"):
        if pname in widget.param:
            params.append(pname)
    return params


def _get_layout_state_params(viewable: Viewable) -> list[str]:
    """
    Get state-relevant parameters for layout components.
    """
    from ..layout import Accordion, Tabs
    if isinstance(viewable, Tabs):
        return ["active"]
    if isinstance(viewable, Accordion):
        return ["active"]
    return []


def _get_pane_state_params(pane: PaneBase) -> list[str]:
    """
    Get serializable state parameters for a pane.
    """
    from ..pane.base import PaneBase
    serializable_params = []
    for pname, pobj in pane.param.objects().items():
        if pname in ("name", "margin", "css_classes", "loading"):
            continue
        if pobj.readonly:
            continue
        try:
            val = getattr(pane, pname)
            _serialize_value(pane, pname, val)
            serializable_params.append(pname)
        except Exception:
            continue
    return serializable_params[:20]


def collect_state(
    root: t.Any = None,
    location: t.Any = None,
    include_location: bool = True,
    include_widgets: bool = True,
    include_layout: bool = True,
    include_panes: bool = True,
) -> dict[str, t.Any]:
    """
    Collect the full dashboard state into a serializable dictionary.

    Parameters
    ----------
    root : Viewable or BaseTemplate or None
        Root component to collect state from. If None, uses state.template
        or falls back to collecting from all active views.
    location : Location or None
        Location instance to collect query params from. If None, uses
        state.location.
    include_location : bool
        Whether to include URL/location state.
    include_widgets : bool
        Whether to include widget parameter values.
    include_layout : bool
        Whether to include layout state (Tabs.active, Accordion.active).
    include_panes : bool
        Whether to include serializable pane state.

    Returns
    -------
    dict
        Serializable state dictionary.
    """
    from ..template.base import BaseTemplate
    state = _get_state()

    snapshot: dict[str, t.Any] = {
        "version": 1,
        "widgets": {},
        "layouts": {},
        "panes": {},
        "location": {},
    }

    if root is None:
        try:
            root = state.template
        except Exception:
            root = None
        if root is None:
            root = state.curdoc
            if root is not None:
                for ref, (view, _, doc, _) in state._views.items():
                    if doc is root:
                        root = view
                        break

    if root is None:
        return snapshot

    viewables = _collect_viewables(root)

    for view in viewables:
        path = _get_viewable_path(view, root)
        if path is None:
            continue

        if include_widgets:
            from ..widgets.base import Widget
            if isinstance(view, Widget):
                widget_state = {}
                for pname in _get_widget_params(view):
                    if pname in view.param:
                        val = getattr(view, pname)
                        serialized = _serialize_value(view, pname, val)
                        if serialized is not None:
                            widget_state[pname] = serialized
                if widget_state:
                    snapshot["widgets"][path] = {
                        "type": type(view).__name__,
                        "params": widget_state,
                    }

        if include_layout:
            layout_params = _get_layout_state_params(view)
            if layout_params:
                layout_state = {}
                for pname in layout_params:
                    if pname in view.param:
                        val = getattr(view, pname)
                        serialized = _serialize_value(view, pname, val)
                        if serialized is not None:
                            layout_state[pname] = serialized
                if layout_state:
                    snapshot["layouts"][path] = {
                        "type": type(view).__name__,
                        "params": layout_state,
                    }

        if include_panes:
            from ..pane.base import PaneBase
            if isinstance(view, PaneBase):
                pane_params = _get_pane_state_params(view)
                if pane_params:
                    pane_state = {}
                    for pname in pane_params:
                        if pname in view.param:
                            val = getattr(view, pname)
                            serialized = _serialize_value(view, pname, val)
                            if serialized is not None:
                                pane_state[pname] = serialized
                    if pane_state:
                        snapshot["panes"][path] = {
                            "type": type(view).__name__,
                            "params": pane_state,
                        }

    if include_location:
        loc = location or state.location
        if loc is not None:
            snapshot["location"] = {
                "search": loc.search,
                "hash": loc.hash,
                "pathname": loc.pathname,
                "query_params": loc.query_params,
            }

    return snapshot


def apply_state(
    snapshot: dict[str, t.Any],
    root: t.Any = None,
    location: t.Any = None,
    apply_location: bool = True,
    apply_widgets: bool = True,
    apply_layout: bool = True,
    apply_panes: bool = True,
) -> dict[str, list[str]]:
    """
    Apply a previously collected state snapshot to the dashboard.

    Parameters
    ----------
    snapshot : dict
        State snapshot dictionary produced by collect_state.
    root : Viewable or BaseTemplate or None
        Root component to apply state to.
    location : Location or None
        Location instance to apply URL state to.
    apply_location : bool
        Whether to apply URL/location state.
    apply_widgets : bool
        Whether to apply widget parameter values.
    apply_layout : bool
        Whether to apply layout state.
    apply_panes : bool
        Whether to apply pane state.

    Returns
    -------
    dict
        Dictionary with keys 'applied' and 'failed' listing paths.
    """
    from ..template.base import BaseTemplate
    from ..widgets.base import Widget
    from ..viewable import Viewable
    state = _get_state()

    result: dict[str, list[str]] = {"applied": [], "failed": []}

    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        return result

    if root is None:
        try:
            root = state.template
        except Exception:
            root = None
        if root is None:
            root = state.curdoc
            if root is not None:
                for ref, (view, _, doc, _) in state._views.items():
                    if doc is root:
                        root = view
                        break

    if root is None:
        return result

    viewables = _collect_viewables(root)
    path_map: dict[str, Viewable] = {}
    for view in viewables:
        path = _get_viewable_path(view, root)
        if path:
            path_map[path] = view

    def _apply_section(
        section_name: str,
        flag: bool,
        type_check: Callable[[t.Any], bool] | None = None,
    ) -> None:
        if not flag:
            return
        section = snapshot.get(section_name, {})
        for path, info in section.items():
            view = path_map.get(path)
            if view is None:
                result["failed"].append(f"{section_name}:{path}")
                continue
            if type_check is not None and not type_check(view):
                result["failed"].append(f"{section_name}:{path}")
                continue
            params = info.get("params", {})
            try:
                updates = {}
                for pname, stored_val in params.items():
                    if pname not in view.param:
                        continue
                    val = _deserialize_value(view, pname, stored_val)
                    if val is not None or stored_val is None:
                        updates[pname] = val
                if updates:
                    with edit_readonly(view):
                        view.param.update(**updates)
                result["applied"].append(f"{section_name}:{path}")
            except Exception:
                result["failed"].append(f"{section_name}:{path}")

    _apply_section("widgets", apply_widgets, lambda v: isinstance(v, Widget))
    _apply_section("layouts", apply_layout)
    _apply_section("panes", apply_panes)

    if apply_location:
        loc = location or state.location
        loc_state = snapshot.get("location", {})
        if loc is not None and loc_state:
            try:
                if "search" in loc_state and loc_state["search"]:
                    loc.search = loc_state["search"]
                if "hash" in loc_state:
                    loc.hash = loc_state["hash"]
                result["applied"].append("location")
            except Exception:
                result["failed"].append("location")

    return result


def encode_snapshot(snapshot: dict[str, t.Any]) -> str:
    """
    Encode a snapshot dict as a URL-safe base64 string (compressed).
    """
    raw = json.dumps(snapshot).encode("utf-8")
    compressed = zlib.compress(raw, level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii")


def decode_snapshot(encoded: str) -> dict[str, t.Any] | None:
    """
    Decode a URL-safe base64 snapshot string back to a dict.
    """
    try:
        compressed = base64.urlsafe_b64decode(encoded.encode("ascii"))
        raw = zlib.decompress(compressed)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def save_named_snapshot(name: str, snapshot: dict[str, t.Any]) -> None:
    """
    Save a named snapshot to the session store.
    """
    _SnapshotStore.save_snapshot(name, snapshot)


def load_named_snapshot(name: str) -> dict[str, t.Any] | None:
    """
    Load a named snapshot from the session store.
    """
    return _SnapshotStore.load_snapshot(name)


def delete_named_snapshot(name: str) -> bool:
    """
    Delete a named snapshot from the session store.
    """
    return _SnapshotStore.delete_snapshot(name)


def list_named_snapshots() -> list[str]:
    """
    List all named snapshots in the session store.
    """
    return _SnapshotStore.list_snapshots()


def get_snapshot_from_url() -> dict[str, t.Any] | None:
    """
    Check if the current URL has a snapshot query parameter and decode it.
    """
    state = _get_state()
    loc = state.location
    if loc is None:
        return None
    query_params = loc.query_params
    encoded = query_params.get(SNAPSHOT_QUERY_PARAM)
    if not encoded:
        return None
    if isinstance(encoded, list):
        encoded = encoded[0] if encoded else None
    if not encoded:
        return None
    return decode_snapshot(encoded)


def apply_url_snapshot() -> dict[str, list[str]] | None:
    """
    Check URL for snapshot parameter and apply it if present.
    Returns the apply result or None if no snapshot found.
    """
    snapshot = get_snapshot_from_url()
    if snapshot is None:
        return None
    return apply_state(snapshot)


__all__ = [
    "SNAPSHOT_QUERY_PARAM",
    "SNAPSHOT_BASE_URL",
    "collect_state",
    "apply_state",
    "encode_snapshot",
    "decode_snapshot",
    "save_named_snapshot",
    "load_named_snapshot",
    "delete_named_snapshot",
    "list_named_snapshots",
    "get_snapshot_from_url",
    "apply_url_snapshot",
]
