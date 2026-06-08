"""
Dashboard state snapshot manager widget.

Provides a UI for saving, loading, deleting, and sharing named snapshots
of dashboard state. All operations go through the unified ``state`` API.

Components that participate in snapshots must set an explicit
``snapshot_key`` parameter.
"""
from __future__ import annotations

import datetime as dt
import typing as t

import param

from ..io.snapshot import SNAPSHOT_QUERY_PARAM
from ..io.state import state
from ..layout import Accordion, Column, Row
from .button import Button
from .input import TextInput
from .misc import JSONEditor
from .select import Select


class SnapshotManager(Column):
    """
    A widget for managing dashboard state snapshots via the unified
    ``state.snapshot()`` / ``state.restore_snapshot()`` API.

    Only components with an explicit ``snapshot_key`` parameter are
    collected into snapshots.

    Place this in a template sidebar or anywhere in your layout:

    >>> sm = pn.widgets.SnapshotManager()
    >>> template.sidebar.append(sm)
    """

    show_editor = param.Boolean(default=False, doc="""
        Whether to show the raw JSON editor for snapshot data.""")

    def __init__(self, **params):
        super().__init__(**params, sizing_mode="stretch_width")

        self._name_input = TextInput(
            placeholder="Snapshot name...",
            sizing_mode="stretch_width",
            margin=(0, 0, 5, 0),
        )

        self._save_btn = Button(
            name="Save",
            button_type="primary",
            width=80,
            margin=(0, 5, 0, 0),
        )
        self._save_btn.on_click(self._on_save)

        self._list_select = Select(
            name="Saved Snapshots",
            options=[],
            sizing_mode="stretch_width",
            margin=(5, 0, 5, 0),
        )
        self._list_select.param.watch(self._on_select, "value")

        self._load_btn = Button(
            name="Load",
            button_type="success",
            width=80,
            margin=(0, 5, 0, 0),
        )
        self._load_btn.on_click(self._on_load)

        self._delete_btn = Button(
            name="Delete",
            button_type="danger",
            width=80,
            margin=(0, 5, 0, 0),
        )
        self._delete_btn.on_click(self._on_delete)

        self._share_btn = Button(
            name="Copy URL",
            button_type="default",
            width=90,
            margin=(0, 0, 0, 0),
        )
        self._share_btn.on_click(self._on_share)

        self._refresh_btn = Button(
            name="Refresh",
            button_type="light",
            width=80,
            margin=(0, 5, 0, 0),
        )
        self._refresh_btn.on_click(self._refresh_list)

        self._status = TextInput(
            value="",
            disabled=True,
            placeholder="Status messages...",
            sizing_mode="stretch_width",
            margin=(5, 0, 0, 0),
        )

        self._json_editor = JSONEditor(
            value={},
            sizing_mode="stretch_width",
            height=200,
            margin=(5, 0, 0, 0),
        )

        self._accordion = Accordion(
            ("Raw Snapshot Data", self._json_editor),
            active=[] if not self.show_editor else [0],
            sizing_mode="stretch_width",
            margin=(5, 0, 0, 0),
        )

        self.extend([
            Row(self._name_input, self._save_btn,
                sizing_mode="stretch_width", margin=(0, 0, 0, 0)),
            self._list_select,
            Row(self._load_btn, self._delete_btn, self._share_btn,
                self._refresh_btn, sizing_mode="stretch_width",
                margin=(0, 0, 5, 0)),
            self._status,
            self._accordion,
        ])

        self._refresh_list()
        state.onload(self._on_session_load)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_status(self, msg: str) -> None:
        self._status.value = msg
        if msg:
            state.log(f"[Snapshot] {msg}")

    def _refresh_list(self, *events) -> None:
        self._list_select.options = state.named_snapshots()
        self._list_select.value = None

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_session_load(self) -> None:
        """Called when the session loads – restores URL-encoded snapshot."""
        from ..io.snapshot import apply_url_snapshot
        try:
            result = apply_url_snapshot()
        except Exception as e:
            self._set_status(f"Error applying URL snapshot: {e}")
            return
        if result is not None:
            applied = len(result.get("applied", []))
            failed = len(result.get("failed", []))
            self._set_status(
                f"Applied URL snapshot: {applied} ok, {failed} failed"
            )

    def _on_save(self, event) -> None:
        name = (self._name_input.value or "").strip()
        if not name:
            name = f"snapshot_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            state.save_named_snapshot(name)
            self._name_input.value = ""
            self._refresh_list()
            self._set_status(f"Saved snapshot: {name}")
        except Exception as e:
            self._set_status(f"Error saving snapshot: {e}")

    def _on_select(self, event) -> None:
        if not event.new:
            return
        try:
            snap = state.load_named_snapshot(event.new)
            if snap is not None:
                self._json_editor.value = snap
        except Exception as e:
            self._set_status(f"Error loading snapshot data: {e}")

    def _on_load(self, event) -> None:
        name = self._list_select.value
        if not name:
            self._set_status("Select a snapshot to load")
            return
        try:
            snap = state.load_named_snapshot(name)
            if snap is None:
                self._set_status(f"Snapshot '{name}' not found")
                return
            result = state.restore_snapshot(snap)
            applied = len(result.get("applied", []))
            failed = len(result.get("failed", []))
            self._set_status(
                f"Loaded '{name}': {applied} ok, {failed} failed"
            )
        except Exception as e:
            self._set_status(f"Error loading snapshot: {e}")

    def _on_delete(self, event) -> None:
        name = self._list_select.value
        if not name:
            self._set_status("Select a snapshot to delete")
            return
        try:
            deleted = state.delete_named_snapshot(name)
            if deleted:
                self._refresh_list()
                self._json_editor.value = {}
                self._set_status(f"Deleted snapshot: {name}")
            else:
                self._set_status(f"Snapshot '{name}' not found")
        except Exception as e:
            self._set_status(f"Error deleting snapshot: {e}")

    def _on_share(self, event) -> None:
        try:
            encoded = state.encode_snapshot()

            loc = state.location
            url: str
            if loc is not None:
                base_href = getattr(loc, "href", None) or ""
                existing_qp = getattr(loc, "query_params", None) or {}
                if SNAPSHOT_QUERY_PARAM in existing_qp:
                    others = [
                        f"{k}={v}" for k, v in existing_qp.items()
                        if k != SNAPSHOT_QUERY_PARAM
                    ]
                    base = base_href.split("?")[0]
                    if others:
                        base_href = base + "?" + "&".join(others)
                    else:
                        base_href = base
                sep = "&" if "?" in base_href else "?"
                url = f"{base_href}{sep}{SNAPSHOT_QUERY_PARAM}={encoded}"
            else:
                url = f"?{SNAPSHOT_QUERY_PARAM}={encoded}"

            try:
                from ..io.notifications import state as _nstate
                notif = _nstate.notifications
                if notif is not None:
                    notif.send(
                        "Snapshot URL ready",
                        "You can share this link.",
                        "info",
                    )
            except Exception:
                pass

            try:
                import pyperclip  # type: ignore
                pyperclip.copy(url)
            except Exception:
                pass

            self._share_url = url
            self._set_status(f"Share URL ready ({len(encoded)} chars)")
        except Exception as e:
            self._set_status(f"Error generating share URL: {e}")
