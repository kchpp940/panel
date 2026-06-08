"""
Dashboard state snapshot functionality.

Components that wish to participate in snapshots must explicitly set
their ``snapshot_key`` parameter. Only components with a non-None
``snapshot_key`` are collected and restored.

Public API:
  - collect_state() -> dict
  - apply_state(snapshot) -> result
  - encode_snapshot(snapshot) -> str
  - decode_snapshot(str) -> dict | None
  - save_named_snapshot(name, snapshot)
  - load_named_snapshot(name) -> dict | None
  - delete_named_snapshot(name) -> bool
  - list_named_snapshots() -> list[str]
  - get_snapshot_from_url() -> dict | None
  - apply_url_snapshot() -> result | None
"""
from __future__ import annotations

import base64
import json
import typing as t
import zlib

from contextlib import suppress
from weakref import WeakKeyDictionary

import param

from bokeh.document import Document

from ..util import edit_readonly

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from ..viewable import Viewable
    from .state import state as _state_type


SNAPSHOT_QUERY_PARAM = "_snapshot"
SNAPSHOT_BASE_URL = "_snapshots"

SNAPSHOT_VERSION = 2


def _get_state():
    from .state import state
    return state


class _SnapshotStore:
    """
    Per-document (session) named snapshot storage.
    Falls back to a process-global store when no Document is available.
    """

    _per_doc: t.ClassVar[WeakKeyDictionary[Document, dict[str, dict[str, t.Any]]]] = (
        WeakKeyDictionary()
    )

    _global: t.ClassVar[dict[str, dict[str, t.Any]]] = {}

    @classmethod
    def _storage(cls, doc: Document | None) -> dict[str, dict[str, t.Any]]:
        if doc is None:
            return cls._global
        if doc not in cls._per_doc:
            cls._per_doc[doc] = {}
        return cls._per_doc[doc]

    @classmethod
    def save(cls, name: str, data: dict[str, t.Any], doc: Document | None = None) -> None:
        cls._storage(doc)[name] = data

    @classmethod
    def load(cls, name: str, doc: Document | None = None) -> dict[str, t.Any] | None:
        return cls._storage(doc).get(name)

    @classmethod
    def delete(cls, name: str, doc: Document | None = None) -> bool:
        storage = cls._storage(doc)
        if name in storage:
            del storage[name]
            return True
        return False

    @classmethod
    def list(cls, doc: Document | None = None) -> list[str]:
        return sorted(cls._storage(doc).keys())


def _find_root_viewables(
    explicit_roots: t.Iterable[t.Any] | None = None,
) -> list[Viewable]:
    """
    Find all root Viewable objects.

    Priority
    --------
    1. *explicit_roots* if provided.
    2. ``state.template`` (plus main/sidebar/header/modal slots).
    3. ``state._views`` entries for the current doc.
    4. ``doc.roots`` that have a ``_panel_viewable`` attribute.
    """
    from ..viewable import Viewable

    state = _get_state()
    roots: list[Viewable] = []
    seen: set[int] = set()

    def _add(obj: t.Any) -> None:
        if isinstance(obj, Viewable) and id(obj) not in seen:
            seen.add(id(obj))
            roots.append(obj)

    if explicit_roots is not None:
        for r in explicit_roots:
            _add(r)
        if roots:
            return roots

    with suppress(Exception):
        tpl = state.template
        if tpl is not None:
            _add(tpl)
            for attr in ("main", "sidebar", "header", "modal"):
                container = getattr(tpl, attr, None)
                _add(container)

    curdoc = state.curdoc
    for ref, (view, _, doc, _) in state._views.items():
        if doc is curdoc or curdoc is None:
            _add(view)

    if curdoc is not None:
        for model in getattr(curdoc, "roots", []) or []:
            _add(getattr(model, "_panel_viewable", None))

    return roots


def _iter_snapshot_components(roots: list[Viewable]) -> t.Iterator[Viewable]:
    """
    Recursively yield Viewable descendants that have snapshot_key set.
    """
    from ..viewable import Viewable

    seen: set[int] = set()

    def _visit(obj: t.Any) -> None:
        if id(obj) in seen:
            return
        seen.add(id(obj))

        if not isinstance(obj, Viewable):
            return

        if getattr(obj, "snapshot_key", None):
            yield obj

        with suppress(Exception):
            for child in obj.select():
                if child is not obj:
                    yield from _visit(child)

        with suppress(Exception):
            for item in getattr(obj, "objects", []) or []:
                yield from _visit(item)

    for root in roots:
        yield from _visit(root)


def _collect_params_for(obj: Viewable) -> dict[str, t.Any]:
    """
    Serialize the relevant parameters for a snapshot-capable component.
    For Widget-like objects: "value" plus a small set of stable params.
    For Tabs/Accordion: "active".
    For other Viewables: try to serialize "value" if present; otherwise
    any JSON-serialisable parameter that isn't layout/style metadata.
    """
    from ..layout import Accordion, Tabs

    out: dict[str, t.Any] = {}

    def _try_serialize(pname: str) -> None:
        if pname not in obj.param:
            return
        pobj = obj.param[pname]
        if getattr(pobj, "readonly", False):
            return
        val = getattr(obj, pname)
        serialized = None
        try:
            if hasattr(pobj, "serialize"):
                candidate = pobj.serialize(val)
                json.dumps(candidate)
                serialized = {"__s__": True, "v": candidate}
        except Exception:
            serialized = None
        if serialized is None:
            try:
                json.dumps(val)
                serialized = val
            except Exception:
                serialized = None
        if serialized is not None:
            out[pname] = serialized

    if isinstance(obj, (Tabs, Accordion)):
        _try_serialize("active")
        return out

    _try_serialize("value")

    for pname in ("active", "disabled", "visible", "options"):
        _try_serialize(pname)

    return out


def _deserialize_params_for(obj: Viewable, params: dict[str, t.Any]) -> dict[str, t.Any]:
    """Inverse of _collect_params_for – returns dict of param updates."""
    updates: dict[str, t.Any] = {}
    for pname, stored in params.items():
        if pname not in obj.param:
            continue
        pobj = obj.param[pname]
        if isinstance(stored, dict) and stored.get("__s__"):
            inner = stored["v"]
            if hasattr(pobj, "deserialize"):
                try:
                    updates[pname] = pobj.deserialize(inner)
                    continue
                except Exception:
                    pass
            updates[pname] = inner
        elif hasattr(pobj, "deserialize_value"):
            try:
                updates[pname] = pobj.deserialize_value(stored)
                continue
            except Exception:
                pass
            updates[pname] = stored
        else:
            updates[pname] = stored
    return updates


def collect_state(root: t.Any = None, *extra_roots: t.Any) -> dict[str, t.Any]:
    """
    Collect a snapshot of all components that have an explicit
    ``snapshot_key`` set, plus the current URL location state.

    Parameters
    ----------
    root : Viewable or iterable of Viewable, optional
        Explicit root(s) to scan. If omitted, the current session's
        template and registered views are used automatically.
    *extra_roots : Viewable
        Additional root objects to scan.

    Returns a dictionary of the form::

        {
            "version": 2,
            "components": {
                "<snapshot_key>": {"type": "...", "params": {...}},
                ...
            },
            "location": {"search": ..., "hash": ..., "query_params": {...}}
        }
    """
    state = _get_state()

    explicit: list[t.Any] = []
    if root is not None:
        if isinstance(root, (list, tuple, set)):
            explicit.extend(root)
        else:
            explicit.append(root)
    explicit.extend(extra_roots)

    roots = _find_root_viewables(explicit if explicit else None)

    snapshot: dict[str, t.Any] = {
        "version": SNAPSHOT_VERSION,
        "components": {},
        "location": {},
    }

    used_keys: set[str] = set()
    for comp in _iter_snapshot_components(roots):
        key = getattr(comp, "snapshot_key", None)
        if not isinstance(key, str) or not key:
            continue
        if key in used_keys:
            continue
        used_keys.add(key)
        params = _collect_params_for(comp)
        if not params:
            continue
        snapshot["components"][key] = {
            "type": type(comp).__name__,
            "params": params,
        }

    loc = state.location
    if loc is not None:
        try:
            snapshot["location"] = {
                "search": loc.search,
                "hash": loc.hash,
                "pathname": getattr(loc, "pathname", ""),
                "query_params": dict(loc.query_params) if loc.query_params else {},
            }
        except Exception:
            pass

    return snapshot


def apply_state(
    snapshot: dict[str, t.Any],
    root: t.Any = None,
    *extra_roots: t.Any,
) -> dict[str, list[str]]:
    """
    Apply a snapshot produced by :func:`collect_state`.

    Parameters
    ----------
    snapshot : dict
        Snapshot dictionary returned by :func:`collect_state`.
    root : Viewable or iterable of Viewable, optional
        Explicit root(s) to scan for matching components.
    *extra_roots : Viewable
        Additional root objects to scan.

    Returns ``{"applied": [...], "failed": [...]}`` listing the
    snapshot_keys that were or were not restored.
    """
    from ..viewable import Viewable

    result: dict[str, list[str]] = {"applied": [], "failed": []}

    if not isinstance(snapshot, dict):
        return result
    version = snapshot.get("version")
    if version not in (1, SNAPSHOT_VERSION):
        return result

    state = _get_state()

    explicit: list[t.Any] = []
    if root is not None:
        if isinstance(root, (list, tuple, set)):
            explicit.extend(root)
        else:
            explicit.append(root)
    explicit.extend(extra_roots)

    roots = _find_root_viewables(explicit if explicit else None)
    key_to_obj: dict[str, Viewable] = {}
    for comp in _iter_snapshot_components(roots):
        key = getattr(comp, "snapshot_key", None)
        if isinstance(key, str) and key and key not in key_to_obj:
            key_to_obj[key] = comp

    components: dict[str, dict[str, t.Any]] = {}
    if version == 1:
        for section in ("widgets", "layouts", "panes"):
            for k, v in snapshot.get(section, {}).items():
                components[k] = v
    else:
        components = snapshot.get("components", {})

    for key, info in components.items():
        obj = key_to_obj.get(key)
        if obj is None:
            result["failed"].append(key)
            continue
        params = info.get("params", {}) if isinstance(info, dict) else {}
        try:
            updates = _deserialize_params_for(obj, params)
            if updates:
                with edit_readonly(obj):
                    obj.param.update(**updates)
            result["applied"].append(key)
        except Exception:
            result["failed"].append(key)

    loc = state.location
    loc_state = snapshot.get("location", {})
    if loc is not None and isinstance(loc_state, dict) and loc_state:
        try:
            if "search" in loc_state and loc_state["search"]:
                loc.search = loc_state["search"]
            if "hash" in loc_state:
                loc.hash = loc_state["hash"]
            result["applied"].append("__location__")
        except Exception:
            result["failed"].append("__location__")

    return result


def encode_snapshot(snapshot: dict[str, t.Any]) -> str:
    """Encode a snapshot dict as a URL-safe base64 string (zlib compressed)."""
    raw = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, level=9)).decode("ascii")


def decode_snapshot(encoded: str) -> dict[str, t.Any] | None:
    """Decode a URL-safe base64 snapshot string back to a dict."""
    try:
        compressed = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return json.loads(zlib.decompress(compressed).decode("utf-8"))
    except Exception:
        return None


def save_named_snapshot(
    name: str,
    snapshot: dict[str, t.Any] | None = None,
    root: t.Any = None,
    *extra_roots: t.Any,
) -> dict[str, t.Any]:
    """
    Save a snapshot under ``name`` in the current session store.
    If ``snapshot`` is None, collect a fresh one via :func:`collect_state`.
    Returns the snapshot that was saved.
    """
    if snapshot is None:
        snapshot = collect_state(root, *extra_roots)
    state = _get_state()
    _SnapshotStore.save(name, snapshot, doc=state.curdoc)
    return snapshot


def load_named_snapshot(name: str) -> dict[str, t.Any] | None:
    """Load a named snapshot from the session store."""
    state = _get_state()
    return _SnapshotStore.load(name, doc=state.curdoc)


def delete_named_snapshot(name: str) -> bool:
    """Delete a named snapshot from the session store."""
    state = _get_state()
    return _SnapshotStore.delete(name, doc=state.curdoc)


def list_named_snapshots() -> list[str]:
    """List all named snapshots in the session store."""
    state = _get_state()
    return _SnapshotStore.list(doc=state.curdoc)


def get_snapshot_from_url() -> dict[str, t.Any] | None:
    """Return the decoded snapshot from the URL query parameter, if any."""
    state = _get_state()
    loc = state.location
    if loc is None:
        return None
    query_params = getattr(loc, "query_params", None) or {}
    encoded = query_params.get(SNAPSHOT_QUERY_PARAM)
    if isinstance(encoded, list):
        encoded = encoded[0] if encoded else None
    if not encoded:
        return None
    return decode_snapshot(encoded)


def apply_url_snapshot() -> dict[str, list[str]] | None:
    """Apply the snapshot encoded in the URL query parameter, if present."""
    snapshot = get_snapshot_from_url()
    if snapshot is None:
        return None
    return apply_state(snapshot)


__all__ = [
    "SNAPSHOT_BASE_URL",
    "SNAPSHOT_QUERY_PARAM",
    "SNAPSHOT_VERSION",
    "apply_state",
    "apply_url_snapshot",
    "collect_state",
    "decode_snapshot",
    "delete_named_snapshot",
    "encode_snapshot",
    "get_snapshot_from_url",
    "list_named_snapshots",
    "load_named_snapshot",
    "save_named_snapshot",
]
