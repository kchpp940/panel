"""
Unified resource locator and path resolution utilities.

This module provides a single source of truth for locating static assets
(JS, CSS, bundled libraries, template/theme resources) across all Panel
execution modes:

  * Editable install (pip install -e .)
  * Local dev build (panel build / npm build)
  * Watch / autoreload mode (panel serve --dev)
  * Installed wheel (site-packages)

It centralizes:
  * Path discovery (where is the ``dist/`` directory really?)
  * Resource resolution (given a logical name, find the file or URL)
  * Cache-busting version suffixes
  * Consistent error messages with recovery hints
"""
from __future__ import annotations

import functools
import importlib
import json
import logging
import os
import pathlib
import typing as t
import uuid

from pathlib import Path

from ..util import isurl

if t.TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Installation-mode detection
# ---------------------------------------------------------------------------

InstallMode = t.Literal['editable', 'wheel', 'unknown']


def _detect_install_mode(panel_root: Path) -> InstallMode:
    """
    Determine whether Panel was installed as an editable install, as a
    regular wheel, or whether we can't tell (e.g. frozen binary).
    """
    try:
        import panel as _panel_mod
    except Exception:
        return 'unknown'
    module_file = getattr(_panel_mod, '__file__', None)
    if not module_file:
        return 'unknown'
    module_dir = Path(module_file).resolve().parent
    # In an editable install the package directory lives next to
    # ``pyproject.toml`` / ``setup.py`` in the source checkout.
    project_root = module_dir.parent
    for marker in ('pyproject.toml', 'setup.py', 'setup.cfg'):
        if (project_root / marker).is_file():
            return 'editable'
    return 'wheel'


def _read_package_version(panel_root: Path) -> str:
    package_json = panel_root / 'package.json'
    if package_json.is_file():
        try:
            with open(package_json, encoding='utf-8') as f:
                return json.load(f)['version'].split('+')[0]
        except Exception:
            pass
    try:
        from ..__version import __version__
        return str(__version__).split('+')[0]
    except Exception:
        return '0.0.0'


# ---------------------------------------------------------------------------
# ResourcePaths — single source of truth for on-disk locations
# ---------------------------------------------------------------------------

class ResourcePaths:
    """
    Holds the canonical on-disk locations for Panel's static assets.

    All directory properties are resolved at construction time and cached
    for the lifetime of the process.  The class is intentionally small
    and dependency-free so it is safe to import very early during
    Panel's startup sequence.
    """

    __slots__ = (
        '_panel_root', '_dist_dir', '_bundle_dir', '_assets_dir',
        '_templates_dir', '_internal_templates_dir',
        '_install_mode', '_js_version',
    )

    def __init__(self) -> None:
        panel_root = Path(__file__).resolve().parent.parent
        self._panel_root = panel_root
        self._dist_dir = panel_root / 'dist'
        self._bundle_dir = self._dist_dir / 'bundled'
        self._assets_dir = panel_root / 'assets'
        self._templates_dir = panel_root / 'template'
        self._internal_templates_dir = panel_root / '_templates'
        self._install_mode = _detect_install_mode(panel_root)
        self._js_version = _read_package_version(panel_root)

    # ------------------------------------------------------------------
    # Directory accessors
    # ------------------------------------------------------------------

    @property
    def panel_root(self) -> Path:
        """Root directory of the ``panel`` Python package."""
        return self._panel_root

    @property
    def dist_dir(self) -> Path:
        """``panel/dist/`` — built JS bundles, CSS and images."""
        return self._dist_dir

    @property
    def bundle_dir(self) -> Path:
        """``panel/dist/bundled/`` — bundled third-party libraries."""
        return self._bundle_dir

    @property
    def assets_dir(self) -> Path:
        """``panel/assets/`` — raw source assets (may not exist in wheels)."""
        return self._assets_dir

    @property
    def templates_dir(self) -> Path:
        """``panel/template/`` — Jinja2 templates shipped with Panel."""
        return self._templates_dir

    @property
    def internal_templates_dir(self) -> Path:
        """``panel/_templates/`` — internal Jinja2 templates."""
        return self._internal_templates_dir

    @property
    def install_mode(self) -> InstallMode:
        """How the Panel package was installed."""
        return self._install_mode

    @property
    def js_version(self) -> str:
        """Panel JS bundle version (from ``package.json`` / ``__version__``)."""
        return self._js_version

    # ------------------------------------------------------------------
    # Existence checks with helpful diagnostics
    # ------------------------------------------------------------------

    def dist_available(self) -> bool:
        """Return ``True`` if the ``dist/`` directory looks populated."""
        return (
            self._dist_dir.is_dir()
            and any(self._dist_dir.iterdir())
        )

    def bundled_file(self, *parts: str) -> Path | None:
        """
        Resolve a bundled file relative to ``dist/bundled/``.

        Returns the resolved ``Path`` if the file exists, otherwise
        ``None``.  Use :meth:`require_bundled_file` to raise instead.
        """
        path = self._bundle_dir.joinpath(*parts)
        return path if path.is_file() else None

    def require_bundled_file(self, *parts: str) -> Path:
        """Like :meth:`bundled_file` but raises on failure."""
        path = self.bundled_file(*parts)
        if path is None:
            raise ResourceNotFoundError.bundled(
                '/'.join(parts), self._bundle_dir
            )
        return path

    def dist_file(self, *parts: str) -> Path | None:
        """Resolve a file relative to ``dist/``, returns None if missing."""
        path = self._dist_dir.joinpath(*parts)
        return path if path.is_file() else None

    def require_dist_file(self, *parts: str) -> Path:
        path = self.dist_file(*parts)
        if path is None:
            raise ResourceNotFoundError.dist('/'.join(parts), self._dist_dir)
        return path

    def template_css_file(self, template_name: str, css_file: str) -> Path | None:
        """Look up a template CSS file in ``dist/bundled/<template>/``."""
        return self.bundled_file(template_name.lower(), css_file)

    def template_js_file(self, template_name: str, js_file: str) -> Path | None:
        """Look up a template JS file in ``dist/bundled/<template>/``."""
        return self.bundled_file(template_name.lower(), js_file)


# Singleton instance — cheap to construct, safe to share
_resource_paths = ResourcePaths()


def get_resource_paths() -> ResourcePaths:
    """Return the process-wide :class:`ResourcePaths` singleton."""
    return _resource_paths


# ---------------------------------------------------------------------------
# URL / path construction
# ---------------------------------------------------------------------------

# Default URL prefixes (overridable via environment / config)
_LOCAL_DIST = "static/extensions/panel/"
_COMPONENT_PATH = "components/"


@functools.lru_cache(maxsize=1)
def _cdn_dist_url() -> str:
    """CDN base URL for Panel dist assets (cached)."""
    from ..config import config as _config
    return f"{_config.cdn_root}{_resource_paths.js_version}/dist/"


def use_cdn_for_resources() -> bool:
    """Whether we should prefer CDN URLs over local paths right now."""
    from bokeh.settings import settings as _bk_settings
    from .state import state as _state
    mode = _bk_settings.resources(default="server")
    return mode != 'server' or _state._is_pyodide


def get_dist_base_url(
    cdn: bool | t.Literal['auto'] = 'auto',
) -> str:
    """
    Return the base URL prefix for ``dist/`` assets.

    The returned value *does not* contain a leading ``./`` — callers
    that need a relative URL for ESM imports should add it themselves.
    """
    from .state import state as _state
    resolved_cdn = use_cdn_for_resources() if cdn == 'auto' else cdn
    if resolved_cdn:
        return _cdn_dist_url()
    if _state.rel_path:
        return f'{_state.rel_path}/{_LOCAL_DIST}'
    return _LOCAL_DIST


def version_suffix() -> str:
    """
    Return the cache-busting query string suffix for a resource URL.

    In autoreload (watch) mode a fresh random token is generated every
    call so that changed CSS/JS is picked up on the next request.
    Otherwise the Panel JS version is used.
    """
    from ..config import config as _config
    if _config.autoreload:
        return f'?v={uuid.uuid4().hex}'
    return f'?v={_resource_paths.js_version}'


def add_version_suffix(url: str, force: bool = False) -> str:
    """Append a version suffix to ``url`` if it does not already have one."""
    if '?' in url and not force:
        return url
    return url + version_suffix()


def file_version_suffix(file_path: str | os.PathLike) -> str:
    """
    Return a cache-busting query string suffix derived from a file's
    mtime (and size).  Used for user-land ESM components where each
    file changes independently of Panel's own release cycle.

    Falls back to the global :func:`version_suffix` if the file cannot
    be stat'd.
    """
    import hashlib
    try:
        st = os.stat(file_path)
        sig = f"{st.st_mtime_ns}:{st.st_size}"
        return f"?v={hashlib.sha256(sig.encode('utf-8')).hexdigest()[:16]}"
    except OSError:
        return version_suffix()


def add_file_version_suffix(url: str, file_path: str | os.PathLike, force: bool = False) -> str:
    """Append a file-specific version suffix to ``url`` if it does not already have one."""
    if '?' in url and not force:
        return url
    return url + file_version_suffix(file_path)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ResourceNotFoundError(FileNotFoundError):
    """
    Raised when a Panel static resource cannot be located on disk.

    The error message includes actionable hints (e.g. run ``panel build``
    when the ``dist/`` directory is missing in an editable install).
    """

    @classmethod
    def bundled(cls, logical_path: str, bundle_dir: Path) -> 'ResourceNotFoundError':
        hint = _build_hint(bundle_dir.parent)
        return cls(
            f"Bundled resource {logical_path!r} not found under {bundle_dir}.\n"
            f"{hint}"
        )

    @classmethod
    def dist(cls, logical_path: str, dist_dir: Path) -> 'ResourceNotFoundError':
        hint = _build_hint(dist_dir.parent)
        return cls(
            f"Dist resource {logical_path!r} not found under {dist_dir}.\n"
            f"{hint}"
        )

    @classmethod
    def component(
        cls,
        component: t.Any,
        attr: str,
        logical_path: str,
    ) -> 'ResourceNotFoundError':
        hint = (
            "Make sure the file exists and is listed in the component's "
            f"{attr!r} attribute."
        )
        name = getattr(component, '__name__', type(component).__name__)
        return cls(
            f"Resource {logical_path!r} declared on {name}.{attr} could not be resolved.\n"
            f"{hint}"
        )


def _build_hint(panel_root: Path) -> str:
    mode = _resource_paths.install_mode
    if mode == 'editable':
        return (
            "It looks like you are using an editable install.  Run "
            "`panel build` (or the equivalent npm build step) from the "
            f"source root {panel_root.parent} to populate the dist/ directory."
        )
    if mode == 'wheel':
        return (
            "Your Panel installation may be incomplete.  Try reinstalling "
            "the package with `pip install --force-reinstall panel`."
        )
    return (
        "Your Panel installation may be incomplete.  Try reinstalling "
        "the package."
    )


# ---------------------------------------------------------------------------
# Component-level resource resolution (replaces resolve_custom_path + friends)
# ---------------------------------------------------------------------------

def resolve_module_path(
    component_or_module: t.Any,
) -> Path | None:
    """
    Return the directory that contains the module ``component_or_module``
    lives in (or the module's own directory).  Returns ``None`` if the
    module has no file-system location (e.g. built-in or frozen).
    """
    if isinstance(component_or_module, str):
        try:
            component_or_module = importlib.import_module(component_or_module)
        except Exception:
            return None
    obj = component_or_module
    if not isinstance(obj, type):
        obj = type(obj)
    try:
        mod = importlib.import_module(obj.__module__)
    except Exception:
        return None
    mod_file = getattr(mod, '__file__', None)
    if not mod_file:
        return None
    module_path = Path(mod_file).parent
    return module_path if module_path.is_dir() else None


def resolve_custom_path(
    component: t.Any,
    path: str | os.PathLike,
    relative: bool = False,
) -> Path | None:
    """
    Resolve ``path`` relative to the module that defines ``component``.

    Mirrors the public API of the original ``resolve_custom_path`` but
    goes through :class:`ResourcePaths` diagnostics when things go
    wrong (logging only — ``None`` is still returned for
    backwards-compatibility with existing call sites).
    """
    if not path:
        return None
    if not isinstance(component, type):
        component = type(component)
    module_path = resolve_module_path(component)
    if module_path is None:
        return None
    path = Path(path)
    abs_path = path if path.is_absolute() else module_path / path
    try:
        if not abs_path.is_file():
            return None
    except OSError:
        return None
    abs_path = Path(os.path.normpath(abs_path.absolute()))
    if not relative:
        return abs_path
    return Path(os.path.relpath(abs_path, module_path))


def component_resource_url(
    component: t.Any,
    attr: str,
    path: str | os.PathLike,
) -> str:
    """
    Build the canonical server URL for a resource declared on a
    component class.  The returned URL is served by the
    ``ComponentResourceHandler`` registered in ``panel.io.server``.
    """
    from bokeh.embed.bundle import extension_dirs
    from .state import state as _state

    if not isinstance(component, type):
        component = type(component)
    base_path = _COMPONENT_PATH

    resolved_path = str(path)
    # If the path lives inside a registered bokeh extension we can serve
    # it from the standard static/extensions/<ext>/ tree instead of going
    # through the generic component handler.
    is_ext = False
    ext_path: Path | None = None
    for ext, dist_dir in extension_dirs.items():
        if _is_subpath(path, dist_dir):
            is_ext = True
            base_path = f'static/extensions/{ext}'
            ext_path = dist_dir
            break

    if _state.rel_path:
        base_path = f"{_state.rel_path}/{base_path}"

    if is_ext and ext_path is not None:
        rel = str(path).replace(str(ext_path.absolute()), '').replace(os.path.sep, '/')
        return f'{base_path}{rel}'

    custom = resolve_custom_path(component, path, relative=True)
    rel_part = os.fspath(custom).replace(os.path.sep, '/') if custom else os.fspath(path)
    return f'{base_path}{component.__module__}/{component.__name__}/{attr}/{rel_part}'


def _is_subpath(path: str | os.PathLike, parent: str | os.PathLike) -> bool:
    path = Path(os.path.normpath(os.path.abspath(path)))
    parent = Path(os.path.normpath(os.path.abspath(parent)))
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Higher-level "resolve this logical resource" helpers
# ---------------------------------------------------------------------------

ResolvedKind = t.Literal['bundled-dist', 'component', 'cdn', 'url', 'inline']


class ResolvedResource(t.NamedTuple):
    """Result of :func:`resolve_resource`."""
    kind: ResolvedKind
    value: str  # Either a server/CDN URL or the raw inline CSS/JS string
    source_path: Path | None = None  # On-disk file when known


def resolve_resource(
    component: t.Any,
    resource_type: str,
    resource: str,
    *,
    cdn: bool | t.Literal['auto'] = 'auto',
) -> ResolvedResource:
    """
    High-level resolver that turns a ``_resources`` entry into either a
    URL or an inline string.

    This consolidates the logic that previously lived inside
    ``ResourceComponent._resolve_resource``,
    ``BaseTemplate.resolve_resources``, and the various ``Design``
    helpers.
    """
    paths = get_resource_paths()
    dist_base = get_dist_base_url(cdn=cdn)

    # 1) Absolute / remote URLs pass through untouched.
    if isurl(resource):
        # Rewrite unpkg.com -> user-configured NPM CDN if applicable
        from ..config import config as _config
        resource = resource.replace('https://unpkg.com', _config.npm_cdn)
        cdn_base = f'{_config.npm_cdn}/@holoviz/panel@{paths.js_version}/dist/'
        if resource.startswith(cdn_base):
            resource = resource.replace(cdn_base, _cdn_dist_url())
        return ResolvedResource(kind='url', value=resource)

    # 2) Strip a CDN_DIST prefix if present (canonicalise to relative name)
    cdn_dist = _cdn_dist_url()
    if resource.startswith(cdn_dist):
        resource = resource[len(cdn_dist):]

    # 3) Check whether the file lives in dist/bundled/
    bundle_candidate = resource
    if bundle_candidate.startswith('bundled/'):
        bundle_candidate = bundle_candidate[len('bundled/'):]
    from ..config import config as _config
    npm_cdn_prefixes = (_config.npm_cdn, 'https://cdn.jsdelivr.net/npm', 'https://unpkg.com')
    if resource.startswith(npm_cdn_prefixes):
        for prefix in npm_cdn_prefixes:
            if resource.startswith(prefix):
                bundle_candidate = resource.replace(prefix, '')[1:]
                break
    bundle_file = paths.bundled_file(bundle_candidate)
    cdn_dist = _cdn_dist_url()
    if bundle_file is not None or (from_state_pyodide() and not isurl(resource)):
        prefixed = f'./{dist_base}' if (
            resource_type == 'js_modules'
            and not (relpath_set() or (cdn is True or (cdn == 'auto' and use_cdn_for_resources())))
        ) else dist_base
        url = f'{prefixed}bundled/{bundle_candidate}'
        if resource_type == 'css':
            url = add_version_suffix(url)
        return ResolvedResource(
            kind='bundled-dist',
            value=url,
            source_path=bundle_file,
        )

    # 3.5) Local bundled file missing — try CDN fallback if CDN mode is allowed
    if use_cdn_for_resources() or cdn is True:
        cdn_url = f'{cdn_dist}bundled/{bundle_candidate}'
        if resource_type == 'css':
            cdn_url = add_version_suffix(cdn_url)
        logger.warning(
            "Bundled resource %s not found locally at %s. Falling back to CDN URL %s. %s",
            bundle_candidate,
            paths.bundle_dir / bundle_candidate,
            cdn_url,
            "Run `panel build` to populate dist/." if paths.install_mode == 'editable' else "",
        )
        return ResolvedResource(
            kind='cdn',
            value=cdn_url,
            source_path=None,
        )

    # 4) Check whether it's a component-level resource
    if resolve_custom_path(component, resource):
        return ResolvedResource(
            kind='component',
            value=component_resource_url(component, f'_resources/{resource_type}', resource),
        )

    # 5) Nothing worked — give a clear, actionable error
    raise ResourceNotFoundError.component(component, f'_resources.{resource_type}', resource)


def from_state_pyodide() -> bool:
    from .state import state as _state
    return _state._is_pyodide


def relpath_set() -> bool:
    from .state import state as _state
    return bool(_state.rel_path)


# ---------------------------------------------------------------------------
# Stylesheet patching (unifies patch_stylesheet)
# ---------------------------------------------------------------------------

def apply_dist_url_to_stylesheet(stylesheet: t.Any, dist_url: str) -> None:
    """
    Rewrite a :class:`bokeh.models.ImportedStyleSheet` URL so it points at
    the correct ``dist_url`` for the current rendering context (local
    server, CDN, or relative URL).

    This replaces the ad-hoc ``patch_stylesheet`` function and is safe
    to call on any object that has a ``url`` attribute (non-matching
    objects are simply left alone).
    """
    try:
        url = stylesheet.url
    except Exception:
        return
    cdn_dist = _cdn_dist_url()
    local_dist = _LOCAL_DIST
    new_url: str | None = None
    if url.startswith(cdn_dist + dist_url) and dist_url != cdn_dist:
        new_url = url.replace(cdn_dist + dist_url, dist_url)
    elif url.startswith(cdn_dist) and dist_url != cdn_dist:
        new_url = url.replace(cdn_dist, dist_url)
    elif url.startswith(local_dist) and dist_url.lstrip('./').startswith(local_dist):
        new_url = url.replace(local_dist, dist_url)
    elif url.startswith(local_dist) and dist_url != local_dist:
        new_url = url.replace(local_dist, dist_url)
    if new_url is None:
        return
    new_url = add_version_suffix(new_url)
    try:
        stylesheet.url = new_url
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public re-exports kept at module level so `from panel.io._resource_locator import ...`
# works cleanly (useful for tests and advanced users).
# ---------------------------------------------------------------------------

__all__ = [
    'InstallMode',
    'LOCAL_DIST',
    'COMPONENT_PATH',
    'ResourceNotFoundError',
    'ResourcePaths',
    'ResolvedKind',
    'ResolvedResource',
    'add_file_version_suffix',
    'add_version_suffix',
    'apply_dist_url_to_stylesheet',
    'component_resource_url',
    'file_version_suffix',
    'from_state_pyodide',
    'get_dist_base_url',
    'get_resource_paths',
    'relpath_set',
    'resolve_custom_path',
    'resolve_module_path',
    'resolve_resource',
    'use_cdn_for_resources',
    'version_suffix',
]

LOCAL_DIST = _LOCAL_DIST
COMPONENT_PATH = _COMPONENT_PATH
