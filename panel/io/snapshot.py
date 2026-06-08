"""
Dashboard state snapshot functionality.

Key strategy (two-tier, stable-by-default):

1. **Explicit key (highest priority)** – set via the ``snapshot_key``
   parameter on any Viewable. These keys are stable across layout changes
   and are the recommended way for authors to identify important widgets.

2. **Structural key (fallback)** – automatically generated for every
   Viewable that has serializable state. It encodes the component's
   position within a specific root (``p:<index_path>:<TypeName>``), so it
   only matches when the same root has an equivalent layout structure.
   This lets default snapshots remain useful *without* the user having
   to annotate every widget, while still preventing cross-root or
   heavily-dynamic-layout mismatches.

Every snapshot entry carries a ``key_source`` tag (``"explicit"`` or
``"structural"``) and, for explicit keys, the corresponding structural
key is also stored as a fallback.

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
import hashlib
import json
import typing as t
import zlib

from contextlib import suppress
from weakref import WeakKeyDictionary

import param

from bokeh.document import Document

from ..util import edit_readonly

if t.TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ..viewable import Viewable
    from .state import state as _state_type


SNAPSHOT_QUERY_PARAM = "_snapshot"
SNAPSHOT_BASE_URL = "_snapshots"

SNAPSHOT_VERSION = 2
_STRUCTURAL_KEY_PREFIX = "p:"


def _get_state():
    from .state import state
    return state


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Structural key generation
# ---------------------------------------------------------------------------

def _structural_key(
    obj: Viewable,
    root: Viewable,
    index_path: tuple[int, ...],
) -> str:
    """
    Build a structural key for *obj* relative to *root*.

    The key encodes the position path and the component type name. The
    prefix ``p:`` avoids colliding with user-supplied explicit keys.
    """
    path_str = "/".join(str(i) for i in index_path)
    type_name = type(obj).__name__
    raw = f"{_STRUCTURAL_KEY_PREFIX}{path_str}:{type_name}"
    # Keep the key compact – structural keys don't need to be human-readable.
    if len(raw) > 120:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        raw = f"{_STRUCTURAL_KEY_PREFIX}{digest}:{type_name}"
    return raw


# ---------------------------------------------------------------------------
# Recursive traversal with index-path tracking
# ---------------------------------------------------------------------------

def _iter_component_tree(
    roots: list[Viewable],
) -> Iterator[tuple[Viewable, Viewable, tuple[int, ...]]]:
    """
    Yield ``(component, root, index_path)`` for every Viewable reachable
    from *roots* by walking ``.objects``/``.select()`` descendants.

    ``index_path`` is the tuple of child indices that locate *component*
    within ``root``.  For the root itself the path is ``()``.
    """
    from ..viewable import Viewable

    seen: set[int] = set()

    def _visit(
        obj: t.Any,
        root: Viewable,
        path: tuple[int, ...],
    ) -> Iterator[tuple[Viewable, Viewable, tuple[int, ...]]]:
        if id(obj) in seen:
            return
        seen.add(id(obj))

        if not isinstance(obj, Viewable):
            return

        yield obj, root, path

        children: list[t.Any] = []
        with suppress(Exception):
            objs = getattr(obj, "objects", None)
            if isinstance(objs, (list, tuple)):
                children.extend(list(objs))

        if not children:
            with suppress(Exception):
                for ch in obj.select():
                    if ch is not obj:
                        children.append(ch)

        for idx, ch in enumerate(children):
            yield from _visit(ch, root, (*path, idx))

    for root in roots:
        yield from _visit(root, root, ())


# ---------------------------------------------------------------------------
# Param serialization
# ---------------------------------------------------------------------------

def _collect_params_for(obj: Viewable) -> dict[str, t.Any]:
    """
    Serialize the relevant parameters for a snapshot-capable component.
    Widget-like objects: ``value`` + a small set of stable params.
    Tabs/Accordion: ``active``.
    Other Viewables: try ``value`` first, then a fixed small list.
    Returns an empty dict when nothing serializable is found.
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

    from ..pane.base import PaneBase

    if isinstance(obj, PaneBase):
        for pname in ("object", "disabled", "visible"):
            _try_serialize(pname)
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


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect_state(root: t.Any = None, *extra_roots: t.Any) -> dict[str, t.Any]:
    """
    Collect a snapshot of all serializable Viewables reachable from the
    supplied roots (or the current session's roots when omitted).

    Components with an explicit ``snapshot_key`` are recorded under that
    key with ``key_source="explicit"``; every other component that has
    serializable state is recorded under an auto-generated structural
    key (``key_source="structural"``). Structural keys are only valid
    within the same root/layout.

    Parameters
    ----------
    root : Viewable or iterable of Viewable, optional
        Explicit root(s) to scan. If omitted the current session's
        template and registered views are used automatically.
    *extra_roots : Viewable
        Additional root objects to scan.

    Returns
    -------
    dict
        ``{version, components, location}``.
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

    used_explicit: set[str] = set()
    used_structural: set[str] = set()

    for comp, comp_root, path in _iter_component_tree(roots):
        # 1) Try the per-component snapshot hook (Plotly/Tabulator/Vega/... custom state)
        custom_state: dict[str, t.Any] | None = None
        try:
            hook_result = comp._get_snapshot_state()
            if isinstance(hook_result, dict):
                # Ensure the custom payload is JSON-serialisable; if not, discard
                # fall back silently so the snapshot remains valid.
                json.dumps(hook_result)
                custom_state = hook_result
        except Exception:
            custom_state = None

        # 2) Fallback/default param-based collector (value, active, ...)
        params = _collect_params_for(comp)

        if not params and custom_state is None:
            continue

        entry: dict[str, t.Any] = {
            "type": type(comp).__name__,
        }
        if custom_state is not None:
            entry["custom"] = custom_state
        if params:
            entry["params"] = params

        explicit_key = getattr(comp, "snapshot_key", None)
        if isinstance(explicit_key, str) and explicit_key and explicit_key not in used_explicit:
            used_explicit.add(explicit_key)
            skey = _structural_key(comp, comp_root, path)
            entry["key_source"] = "explicit"
            entry["structural_key"] = skey
            snapshot["components"][explicit_key] = entry
            used_structural.add(skey)
            continue

        skey = _structural_key(comp, comp_root, path)
        if skey in used_structural:
            continue
        used_structural.add(skey)
        entry["key_source"] = "structural"
        snapshot["components"][skey] = entry

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


# ---------------------------------------------------------------------------
# Restoration
# ---------------------------------------------------------------------------

def apply_state(
    snapshot: dict[str, t.Any],
    root: t.Any = None,
    *extra_roots: t.Any,
) -> dict[str, list[str]]:
    """
    Apply a snapshot produced by :func:`collect_state`.

    Matching order for each snapshot entry:

    * ``key_source="explicit"`` – first look up the component by its
      explicit key in the current tree; if that fails, fall back to the
      stored ``structural_key`` (same-root positional match).
    * ``key_source="structural"`` – only the structural (positional)
      match is attempted, avoiding cross-root mismatches.

    Parameters
    ----------
    snapshot : dict
        Snapshot dictionary returned by :func:`collect_state`.
    root : Viewable or iterable of Viewable, optional
        Explicit root(s) to scan for matching components.
    *extra_roots : Viewable
        Additional root objects to scan.

    Returns
    -------
    dict
        ``{"applied": [...], "failed": [...]}``.
    """
    from ..viewable import Viewable

    result: dict[str, list[str]] = {"applied": [], "failed": []}

    if not isinstance(snapshot, dict):
        return result
    version = snapshot.get("version")
    if version not in (1, SNAPSHOT_VERSION):
        return result

    state = _get_state()

    explicit_in: list[t.Any] = []
    if root is not None:
        if isinstance(root, (list, tuple, set)):
            explicit_in.extend(root)
        else:
            explicit_in.append(root)
    explicit_in.extend(extra_roots)

    roots = _find_root_viewables(explicit_in if explicit_in else None)

    # Build two indexes of the live tree.
    explicit_map: dict[str, Viewable] = {}
    structural_map: dict[str, Viewable] = {}
    for comp, comp_root, path in _iter_component_tree(roots):
        ekey = getattr(comp, "snapshot_key", None)
        if isinstance(ekey, str) and ekey and ekey not in explicit_map:
            explicit_map[ekey] = comp
        skey = _structural_key(comp, comp_root, path)
        if skey not in structural_map:
            structural_map[skey] = comp

    # Normalize legacy (v1) format
    components: dict[str, dict[str, t.Any]] = {}
    if version == 1:
        for section in ("widgets", "layouts", "panes"):
            for k, v in snapshot.get(section, {}).items():
                components[k] = dict(v, key_source="explicit")
    else:
        components = snapshot.get("components", {})

    for key, info in components.items():
        if not isinstance(info, dict):
            result["failed"].append(key)
            continue

        key_source = info.get("key_source", "explicit")
        params = info.get("params", {})
        custom = info.get("custom")

        obj: Viewable | None = None
        if key_source == "explicit":
            obj = explicit_map.get(key)
            if obj is None:
                fallback_skey = info.get("structural_key")
                if isinstance(fallback_skey, str):
                    obj = structural_map.get(fallback_skey)
        else:  # structural
            obj = structural_map.get(key)

        if obj is None:
            result["failed"].append(key)
            continue

        try:
            applied_any = False
            # 1) Component-specific snapshot hook (viewport, selection, ...)
            if isinstance(custom, dict):
                try:
                    obj._apply_snapshot_state(custom)
                    applied_any = True
                except Exception:
                    pass
            # 2) Default param-based restore (value, active, ...)
            updates = _deserialize_params_for(obj, params)
            if updates:
                with edit_readonly(obj):
                    obj.param.update(**updates)
                applied_any = True
            if applied_any:
                result["applied"].append(key)
            else:
                result["failed"].append(key)
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


# ---------------------------------------------------------------------------
# Encode / decode
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Named snapshots
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# URL integration
# ---------------------------------------------------------------------------

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
