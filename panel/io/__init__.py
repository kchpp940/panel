"""
The io module contains utilities for loading JS components, embedding
model state, and rendering panel objects.
"""
import sys

from .cache import cache  # noqa
from .callbacks import PeriodicCallback  # noqa
from .diagnostics import (  # noqa
    DiagnosticContext, DiagnosticIssue, DiagnosticSeverity,
    ServiceDiagnostic, StartupConfig, StartupDiagnosticResult,
    StartupError, StartupMode,
    run_startup_diagnostics, validate_startup,
)
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
    "DiagnosticContext",
    "DiagnosticIssue",
    "DiagnosticSeverity",
    "JSCode",
    "PeriodicCallback",
    "Resources",
    "ServiceDiagnostic",
    "StartupConfig",
    "StartupDiagnosticResult",
    "StartupError",
    "StartupMode",
    "hold",
    "immediate_dispatch",
    "ipywidget",
    "panel_logger",
    "profile",
    "push",
    "push_notebook",
    "run_startup_diagnostics",
    "serve",
    "state",
    "unlocked",
    "validate_startup",
    "with_lock"
)
