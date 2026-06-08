"""
REST API handlers for dashboard state snapshots.

All operations go through the unified ``state.snapshot()`` /
``state.restore_snapshot()`` API.
"""
from __future__ import annotations

import json
import typing as t
from urllib.parse import parse_qs

from tornado import web

from .snapshot import SNAPSHOT_BASE_URL, SNAPSHOT_QUERY_PARAM
from .state import state


class SnapshotHandler(web.RequestHandler):
    """
    REST handler for named and ad-hoc snapshots, backed by the
    unified ``state`` snapshot API.

    Routes
    ------
    GET    /_snapshots/              List named snapshots
    GET    /_snapshots/{name}        Load a named snapshot
    POST   /_snapshots/{name}        Save (collect) current state as a named snapshot
    DELETE /_snapshots/{name}        Delete a named snapshot
    POST   /_snapshots/snapshot      Collect current state (returns raw snapshot)
    POST   /_snapshots/restore       Apply a snapshot (JSON body ``{"snapshot": ...}``
                                     or ``{"encoded": "..."}``)
    POST   /_snapshots/encode        Encode current state (or body snapshot) to URL-safe string
    POST   /_snapshots/decode        Decode a URL-safe snapshot string
    """

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_body(request_body: bytes) -> dict:
        try:
            return json.loads(request_body) if request_body else {}
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------
    # HTTP verbs
    # ------------------------------------------------------------------

    def get(self, path: str = ""):
        path = path.strip("/")

        if path == "":
            self.write(json.dumps({"snapshots": state.named_snapshots()}))
            return

        snap = state.load_named_snapshot(path)
        if snap is None:
            self.set_status(404)
            self.write(json.dumps({"error": f"Snapshot '{path}' not found"}))
            return
        self.write(json.dumps({"name": path, "snapshot": snap}))

    def post(self, path: str = ""):
        path = path.strip("/")
        body = self._parse_body(self.request.body)

        if path in ("snapshot", "collect"):
            self.write(json.dumps({"snapshot": state.snapshot()}))
            return

        if path == "restore":
            snap = body.get("snapshot")
            encoded = body.get("encoded")
            if snap is None and encoded:
                snap = state.decode_snapshot(encoded)
            if snap is None:
                self.set_status(400)
                self.write(json.dumps({"error": "No snapshot provided"}))
                return
            result = state.restore_snapshot(snap)
            self.write(json.dumps({"result": result}))
            return

        if path == "encode":
            snap = body.get("snapshot")
            encoded = state.encode_snapshot(snap)
            self.write(json.dumps({"encoded": encoded}))
            return

        if path == "decode":
            encoded = body.get("encoded")
            if not encoded:
                args = parse_qs(self.request.query)
                enc_list = args.get("encoded")
                encoded = enc_list[0] if enc_list else None
            if not encoded:
                self.set_status(400)
                self.write(json.dumps({"error": "No encoded snapshot provided"}))
                return
            snap = state.decode_snapshot(encoded)
            if snap is None:
                self.set_status(400)
                self.write(json.dumps({"error": "Invalid encoded snapshot"}))
                return
            self.write(json.dumps({"snapshot": snap}))
            return

        if path:
            snap = body.get("snapshot")
            saved = state.save_named_snapshot(path, snap)
            self.write(json.dumps({"saved": path, "snapshot": saved}))
            return

        self.set_status(400)
        self.write(json.dumps({"error": "Invalid endpoint"}))

    def delete(self, path: str = ""):
        path = path.strip("/")
        if not path:
            self.set_status(400)
            self.write(json.dumps({"error": "Snapshot name required"}))
            return

        deleted = state.delete_named_snapshot(path)
        if deleted:
            self.write(json.dumps({"deleted": path}))
        else:
            self.set_status(404)
            self.write(json.dumps({"error": f"Snapshot '{path}' not found"}))


def snapshot_rest_provider(endpoint: str = SNAPSHOT_BASE_URL):
    """
    Create Tornado routing patterns for the snapshot REST API.

    Parameters
    ----------
    endpoint : str
        The base URL endpoint (default ``'_snapshots'``).

    Returns
    -------
    list
        List of Tornado routing patterns.
    """
    if endpoint and not endpoint.endswith("/"):
        endpoint += "/"
    return [(rf"/{endpoint}(.*)", SnapshotHandler)]
