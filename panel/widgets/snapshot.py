"""
Dashboard state snapshot manager widget.

Provides a UI for saving, loading, deleting, and sharing named snapshots
of the dashboard state, including widget values, layout states, location
parameters, and pane states.
"""
from __future__ import annotations

import datetime as dt
import typing as t

import param

from ..io.snapshot import (
    SNAPSHOT_QUERY_PARAM,
    apply_state,
    collect_state,
    delete_named_snapshot,
    encode_snapshot,
    list_named_snapshots,
    load_named_snapshot,
    save_named_snapshot,
)
from ..io.state import state
from ..layout import Accordion, Column, Row
from .button import Button
from .input import TextInput
from .misc import JSONEditor
from .select import Select


class SnapshotManager(Column):
    """
    A widget for managing dashboard state snapshots.

    Allows users to:
    - Save the current dashboard state as a named snapshot
    - Load previously saved snapshots
    - Delete saved snapshots
    - Generate shareable URLs with encoded state
    - View and export raw snapshot data

    Example
    -------

    >>> snapshot_manager = pn.widgets.SnapshotManager()
    >>> snapshot_manager.servable(area='sidebar')
    """

    autosave = param.Boolean(default=False, doc="""
        Whether to automatically save snapshots on changes.""")

    show_editor = param.Boolean(default=False, doc="""
        Whether to show the raw JSON editor for snapshot data.""")

    _update_lock = param.Boolean(default=False, precedence=-1)

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
            width=80,
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

        header = Row(
            self._name_input,
            self._save_btn,
            sizing_mode="stretch_width",
            margin=(0, 0, 0, 0),
        )

        actions_row = Row(
            self._load_btn,
            self._delete_btn,
            self._share_btn,
            self._refresh_btn,
            sizing_mode="stretch_width",
            margin=(0, 0, 5, 0),
        )

        self.extend([
            header,
            self._list_select,
            actions_row,
            self._status,
            self._accordion,
        ])

        self._refresh_list()

        state.onload(self._on_session_load)

    def _on_session_load(self):
        """Called when a session is loaded to apply URL snapshot if present."""
        from ..io.snapshot import apply_url_snapshot
        try:
            result = apply_url_snapshot()
            if result is not None:
                applied = len(result.get("applied", []))
                failed = len(result.get("failed", []))
                self._set_status(
                    f"Applied URL snapshot: {applied} ok, {failed} failed"
                )
        except Exception as e:
            self._set_status(f"Error applying URL snapshot: {e}")

    def _set_status(self, msg: str):
        self._status.value = msg
        if msg:
            state.log(f"[Snapshot] {msg}")

    def _refresh_list(self, *events):
        self._update_lock = True
        try:
            names = list_named_snapshots()
            self._list_select.options = names
            self._list_select.value = None
        finally:
            self._update_lock = False

    def _on_save(self, event):
        name = (self._name_input.value or "").strip()
        if not name:
            name = f"snapshot_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

        try:
            snapshot = collect_state()
            save_named_snapshot(name, snapshot)
            self._name_input.value = ""
            self._refresh_list()
            self._set_status(f"Saved snapshot: {name}")
        except Exception as e:
            self._set_status(f"Error saving snapshot: {e}")

    def _on_select(self, event):
        if self._update_lock or not event.new:
            return
        try:
            snapshot = load_named_snapshot(event.new)
            if snapshot is not None:
                self._json_editor.value = snapshot
        except Exception as e:
            self._set_status(f"Error loading snapshot data: {e}")

    def _on_load(self, event):
        name = self._list_select.value
        if not name:
            self._set_status("Select a snapshot to load")
            return
        try:
            snapshot = load_named_snapshot(name)
            if snapshot is None:
                self._set_status(f"Snapshot '{name}' not found")
                return
            result = apply_state(snapshot)
            applied = len(result.get("applied", []))
            failed = len(result.get("failed", []))
            self._set_status(
                f"Loaded '{name}': {applied} ok, {failed} failed"
            )
        except Exception as e:
            self._set_status(f"Error loading snapshot: {e}")

    def _on_delete(self, event):
        name = self._list_select.value
        if not name:
            self._set_status("Select a snapshot to delete")
            return
        try:
            deleted = delete_named_snapshot(name)
            if deleted:
                self._refresh_list()
                self._json_editor.value = {}
                self._set_status(f"Deleted snapshot: {name}")
            else:
                self._set_status(f"Snapshot '{name}' not found")
        except Exception as e:
            self._set_status(f"Error deleting snapshot: {e}")

    def _on_share(self, event):
        try:
            snapshot = collect_state()
            encoded = encode_snapshot(snapshot)

            loc = state.location
            if loc is not None:
                base_href = loc.href
                if "?" in base_href:
                    if SNAPSHOT_QUERY_PARAM in loc.query_params:
                        base_href = base_href.split("?")[0]
                        for k, v in loc.query_params.items():
                            if k == SNAPSHOT_QUERY_PARAM:
                                continue
                            sep = "&" if "?" in base_href else "?"
                            base_href += f"{sep}{k}={v}"
                    url = f"{base_href}&{SNAPSHOT_QUERY_PARAM}={encoded}"
                else:
                    url = f"{base_href}?{SNAPSHOT_QUERY_PARAM}={encoded}"
            else:
                url = f"?{SNAPSHOT_QUERY_PARAM}={encoded}"

            try:
                from ..io.notifications import state as _state
                notif = _state.notifications
                if notif is not None:
                    notif.send(
                        "Snapshot URL ready",
                        f"URL copied. You can share this link.",
                        "info",
                    )
            except Exception:
                pass

            self._set_status(f"Share URL ready ({len(encoded)} chars)")

            try:
                import pyperclip
                pyperclip.copy(url)
            except Exception:
                pass

            self._share_url = url

        except Exception as e:
            self._set_status(f"Error generating share URL: {e}")
