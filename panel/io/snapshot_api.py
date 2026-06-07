"""
REST API handlers for dashboard state snapshots.
"""
from __future__ import annotations

import json
import typing as t
from urllib.parse import parse_qs

from tornado import web

from .snapshot import (
    SNAPSHOT_BASE_URL,
    SNAPSHOT_QUERY_PARAM,
    apply_state,
    collect_state,
    decode_snapshot,
    delete_named_snapshot,
    encode_snapshot,
    list_named_snapshots,
    load_named_snapshot,
    save_named_snapshot,
)


class SnapshotHandler(web.RequestHandler):
    """
    REST handler for named snapshots.

    Routes:
    GET    /_snapshots/              List all named snapshots
    GET    /_snapshots/{name}        Load a named snapshot
    POST   /_snapshots/{name}        Save current state as named snapshot
    DELETE /_snapshots/{name}        Delete a named snapshot
    POST   /_snapshots/collect       Collect current state (returns raw snapshot)
    POST   /_snapshots/apply         Apply a snapshot (JSON body or encoded param)
    POST   /_snapshots/encode        Encode a snapshot dict to URL-safe string
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

    def get(self, path: str = ""):
        path = path.strip("/")

        if path == "":
            names = list_named_snapshots()
            self.write(json.dumps({"snapshots": names}))
            return

        snapshot = load_named_snapshot(path)
        if snapshot is None:
            self.set_status(404)
            self.write(json.dumps({"error": f"Snapshot '{path}' not found"}))
            return

        self.write(json.dumps({"name": path, "snapshot": snapshot}))

    def post(self, path: str = ""):
        path = path.strip("/")

        if path == "collect":
            snapshot = collect_state()
            self.write(json.dumps({"snapshot": snapshot}))
            return

        if path == "apply":
            try:
                body = json.loads(self.request.body) if self.request.body else {}
            except json.JSONDecodeError:
                body = {}

            snapshot = body.get("snapshot")
            encoded = body.get("encoded")

            if snapshot is None and encoded:
                snapshot = decode_snapshot(encoded)

            if snapshot is None:
                self.set_status(400)
                self.write(json.dumps({"error": "No snapshot provided"}))
                return

            result = apply_state(snapshot)
            self.write(json.dumps({"result": result}))
            return

        if path == "encode":
            try:
                body = json.loads(self.request.body) if self.request.body else {}
            except json.JSONDecodeError:
                body = {}

            snapshot = body.get("snapshot")
            if snapshot is None:
                snapshot = collect_state()

            encoded = encode_snapshot(snapshot)
            self.write(json.dumps({"encoded": encoded}))
            return

        if path == "decode":
            try:
                body = json.loads(self.request.body) if self.request.body else {}
            except json.JSONDecodeError:
                body = {}

            encoded = body.get("encoded")
            if not encoded:
                args = parse_qs(self.request.query)
                encoded_list = args.get("encoded")
                encoded = encoded_list[0] if encoded_list else None

            if not encoded:
                self.set_status(400)
                self.write(json.dumps({"error": "No encoded snapshot provided"}))
                return

            snapshot = decode_snapshot(encoded)
            if snapshot is None:
                self.set_status(400)
                self.write(json.dumps({"error": "Invalid encoded snapshot"}))
                return

            self.write(json.dumps({"snapshot": snapshot}))
            return

        if path:
            try:
                body = json.loads(self.request.body) if self.request.body else {}
            except json.JSONDecodeError:
                body = {}

            snapshot = body.get("snapshot")
            if snapshot is None:
                snapshot = collect_state()

            save_named_snapshot(path, snapshot)
            self.write(json.dumps({"saved": path, "snapshot": snapshot}))
            return

        self.set_status(400)
        self.write(json.dumps({"error": "Invalid endpoint"}))

    def delete(self, path: str = ""):
        path = path.strip("/")
        if not path:
            self.set_status(400)
            self.write(json.dumps({"error": "Snapshot name required"}))
            return

        deleted = delete_named_snapshot(path)
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
        The base URL endpoint (default: '_snapshots')

    Returns
    -------
    list
        List of Tornado routing patterns.
    """
    if endpoint and not endpoint.endswith("/"):
        endpoint += "/"
    return [
        (rf"/{endpoint}(.*)", SnapshotHandler),
    ]
