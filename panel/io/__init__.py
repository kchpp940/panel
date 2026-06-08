"""
The io module contains utilities for loading JS components, embedding
model state, and rendering panel objects.
"""
import sys
import typing as t

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
    "InteractionStore",
    "PeriodicCallback",
    "Resources",
    "apply_event_filters_to_df",
    "apply_filter",
    "apply_filters_to_df",
    "hold",
    "immediate_dispatch",
    "ipywidget",
    "panel_logger",
    "profile",
    "push",
    "push_notebook",
    "serve",
    "state",
    "unlocked",
    "with_lock"
)

def __getattr__(name: str) -> t.Any:
    if name == "InteractionStore":
        from .interaction_store import InteractionStore
        return InteractionStore
    if name in ("apply_filter", "apply_filters_to_df", "apply_event_filters_to_df"):
        from .interaction_store import (
            apply_event_filters_to_df as _aef,
            apply_filter as _af,
            apply_filters_to_df as _afdf,
        )
        mapping = {
            "apply_filter": _af,
            "apply_filters_to_df": _afdf,
            "apply_event_filters_to_df": _aef,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

if t.TYPE_CHECKING:
    from .interaction_store import (
        InteractionStore,
        apply_event_filters_to_df,
        apply_filter,
        apply_filters_to_df,
    )
