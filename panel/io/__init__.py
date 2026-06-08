"""
The io module contains utilities for loading JS components, embedding
model state, and rendering panel objects.
"""
import sys

from .cache import cache  # noqa
from .callbacks import PeriodicCallback  # noqa
from .document import (  # noqa
    hold, immediate_dispatch, init_doc, unlocked, with_lock,
)
from .embed import embed_state  # noqa
from .logging import panel_logger  # noqa
from .model import (  # noqa
    JSCode, add_to_doc, diff, remove_root,
)
from .notebook import (  # noqa
    _jupyter_server_extension_paths, block_comm, ipywidget, load_notebook,
    push, push_notebook,
)
from .profile import profile  # noqa
from .resources import Resources  # noqa
from .snapshot import (  # noqa
    SNAPSHOT_BASE_URL, SNAPSHOT_QUERY_PARAM, SNAPSHOT_VERSION, apply_state,
    apply_url_snapshot, collect_state, decode_snapshot, delete_named_snapshot,
    encode_snapshot, get_snapshot_from_url, list_named_snapshots,
    load_named_snapshot, save_named_snapshot,
)
from .state import state  # noqa

if state._is_pyodide:
    from .pyodide import serve
else:
    from .server import serve  # noqa
    if 'django' in sys.modules:
        try:
            from . import django  # noqa
        except ImportError:
            pass

__all__ = (
    "JSCode",
    "PeriodicCallback",
    "Resources",
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
    "hold",
    "immediate_dispatch",
    "ipywidget",
    "list_named_snapshots",
    "load_named_snapshot",
    "panel_logger",
    "profile",
    "push",
    "push_notebook",
    "save_named_snapshot",
    "serve",
    "state",
    "unlocked",
    "with_lock"
)
