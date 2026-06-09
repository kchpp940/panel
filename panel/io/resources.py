"""
Patches bokeh resources to make it easy to add external JS and CSS
resources via the panel.config object.

All low-level resource-path discovery is delegated to
``panel.io._resource_locator``.  This module keeps the high-level,
public-facing APIs (``Resources``, ``ResourceComponent``, etc.) and the
Tornado/Jinja helpers that Panel's server layer depends on.
"""
from __future__ import annotations

import functools
import json
import logging
import mimetypes
import os
import pathlib
import re
import textwrap
import typing as t

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import bokeh.embed.wrappers

from bokeh.embed.bundle import (
    CSS_RESOURCES as BkCSS_RESOURCES, URL, Bundle as BkBundle, _any,
    _bundle_extensions, _use_mathjax, bundle_models, extension_dirs,
)
from bokeh.model import Model
from bokeh.models import ImportedStyleSheet
from bokeh.resources import Resources as BkResources, _get_server_urls
from bokeh.settings import _Unset, settings as _settings
from jinja2.environment import Environment
from jinja2.loaders import FileSystemLoader
from markupsafe import Markup

from ..config import config, panel_extension as extension
from ..util import _descendents, isurl, url_path
from ._resource_locator import (
    ResourceNotFoundError,
    ResolvedResource,
    add_version_suffix,
    apply_dist_url_to_stylesheet,
    component_resource_url,
    dist_file_exists,
    get_dist_base_url,
    get_resource_paths,
    read_dist_text,
    resolve_custom_path as _locator_resolve_custom_path,
    resolve_resource,
    use_cdn_for_resources,
    version_suffix as _version_suffix,
)
from .state import state

if t.TYPE_CHECKING:
    from bokeh.resources import Urls

    class TarballType(t.TypedDict, total=False):
        tar: str
        src: str
        dest: str
        exclude: list[str]

    class ResourcesType(t.TypedDict, total=False):
        css: dict[str, str]
        font: dict[str, str]
        js: dict[str, str]
        js_modules: dict[str, str]
        raw_css: list[str]
        tarball: dict[str, TarballType]
        bundle: bool

    MODES = t.Literal['inline', 'cdn', 'server', 'relative', 'absolute', 'server-dev', 'relative-dev', 'absolute-dev']

logger = logging.getLogger(__name__)

ResourceAttr = t.Literal["__css__", "__javascript__"]

_resource_paths = get_resource_paths()
JS_VERSION = _resource_paths.js_version
PANEL_DIR = _resource_paths.panel_root
DIST_DIR = _resource_paths.dist_dir
BUNDLE_DIR = _resource_paths.bundle_dir
ASSETS_DIR = _resource_paths.assets_dir


def get_env():
    ''' Get the correct Jinja2 Environment, also for frozen scripts.
    '''
    return Environment(loader=FileSystemLoader([
        str(_resource_paths.internal_templates_dir.resolve()),
        str(_resource_paths.templates_dir.resolve()),
    ]))

def conffilter(value):
    return json.dumps(dict(value)).replace('"', '\'')

class json_dumps(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, URL):
            return str(obj)
        return super().default(obj)

_env = get_env()
_env.trim_blocks = True
_env.lstrip_blocks = True
_env.filters['json'] = lambda obj: Markup(json.dumps(obj, cls=json_dumps))
_env.filters['conffilter'] = conffilter
_env.filters['sorted'] = sorted

@functools.cache
def parse_template(*args, **kwargs):
    return _env.from_string(*args, **kwargs)

# Handle serving of the panel extension before session is loaded
RESOURCE_MODE: MODES = 'server'
INDEX_TEMPLATE = _env.get_template('convert_index.html')
BASE_TEMPLATE = _env.get_template('base.html')
ERROR_TEMPLATE = _env.get_template('error.html')
LOGOUT_TEMPLATE = _env.get_template('logout.html')
BASIC_LOGIN_TEMPLATE = _env.get_template('basic_login.html')
DEFAULT_TITLE = "Panel Application"
JS_RESOURCES = _env.get_template('js_resources.html')
CDN_ROOT = config.cdn_root
CDN_URL = f"{CDN_ROOT}{JS_VERSION}/"
CDN_DIST = f"{CDN_URL}dist/"
DOC_DIST = "https://panel.holoviz.org/_static/"
from ._resource_locator import LOCAL_DIST, COMPONENT_PATH

BK_PREFIX_RE = re.compile(r'\.bk\.')

# Maps between extension dist directories and CDN
EXTENSION_CDN: dict[str | Path, str] = {}

RESOURCE_URLS = {
    'font-awesome': {
        'zip': 'https://use.fontawesome.com/releases/v5.15.4/fontawesome-free-5.15.4-web.zip',
        'src': 'fontawesome-free-5.15.4-web/',
        'exclude': ['*.svg', '*.scss', '*.less']
    },
    'bootstrap4': {
        'tar': 'https://registry.npmjs.org/bootstrap/-/bootstrap-4.6.1.tgz',
        'src': 'package/dist',
        'exclude': [],
        'dest': ''
    },
    'bootstrap5': {
        'tar': 'https://registry.npmjs.org/bootstrap/-/bootstrap-5.3.0-alpha1.tgz',
        'src': 'package/dist',
        'exclude': [],
        'dest': ''
    },
    'jQuery': {
        'tar': 'https://registry.npmjs.org/jquery/-/jquery-3.5.1.tgz',
        'src': 'package/dist',
        'exclude': [],
        'dest': ''
    }
}

CSS_URLS = {
    'font-awesome': f'{CDN_DIST}bundled/font-awesome/css/all.min.css',
    'bootstrap4': f'{CDN_DIST}bundled/bootstrap4/css/bootstrap.min.css',
    'bootstrap5': f'{CDN_DIST}bundled/bootstrap5/css/bootstrap.min.css'
}

JS_URLS = {
    'jQuery': f'{CDN_DIST}bundled/jquery/jquery.min.js',
    'bootstrap4': f'{CDN_DIST}bundled/bootstrap4/js/bootstrap.bundle.min.js',
    'bootstrap5': f'{CDN_DIST}bundled/bootstrap5/js/bootstrap.bundle.min.js'
}

extension_dirs['panel'] = DIST_DIR

bokeh.embed.wrappers._ONLOAD = """\
(function() {
  const fn = function() {
%(code)s
  };
  if (document.readyState != "loading") fn();
else document.addEventListener("DOMContentLoaded", fn, {once: true});
})();\
"""

mimetypes.add_type("application/javascript", ".js")

@contextmanager
def set_resource_mode(mode: MODES | None):
    global RESOURCE_MODE
    if mode is None:
        mode = RESOURCE_MODE
    old_resources = _settings.resources._user_value
    old_mode = RESOURCE_MODE
    _settings.resources = RESOURCE_MODE = mode
    try:
        yield
    finally:
        RESOURCE_MODE = old_mode
        if old_resources is _Unset:
            _settings.resources.unset_value()
        else:
            _settings.resources.set_value(old_resources)  # type: ignore

def use_cdn() -> bool:
    return use_cdn_for_resources()

def get_dist_path(cdn: bool | t.Literal['auto'] = 'auto') -> str:
    return get_dist_base_url(cdn=cdn)

def is_cdn_url(url: str) -> bool:
    return isurl(url) and url.startswith(CDN_DIST)

def process_raw_css(raw_css: list[str]) -> list[str]:
    """
    Converts old-style Bokeh<3 compatible CSS to Bokeh 3 compatible CSS.
    """
    return [BK_PREFIX_RE.sub('.', css) for css in raw_css]

@lru_cache(maxsize=None)
def loading_css(loading_spinner: str, color: str, max_height: int):
    return textwrap.dedent(f"""
    :host(.pn-loading):before, .pn-loading:before {{
      background-color: {color};
      mask-size: auto calc(min(50%, {max_height}px));
      -webkit-mask-size: auto calc(min(50%, {max_height}px));
    }}""")

def resolve_custom_path(
    obj, path: str | os.PathLike, relative: bool = False
) -> Path | None:
    return _locator_resolve_custom_path(obj, path, relative=relative)

def component_resource_path(component, attr: str, path: str | os.PathLike) -> str:
    return component_resource_url(component, attr, path)

def patch_stylesheet(stylesheet, dist_url):
    apply_dist_url_to_stylesheet(stylesheet, dist_url)

def _is_file_path(stylesheet: str)->bool:
    return stylesheet.lower().endswith(".css")

def _is_subpath(path: str | os.PathLike, parent: str | os.PathLike) -> bool:
    from ._resource_locator import _is_subpath as _impl
    return _impl(path, parent)

def resolve_resource_cdn(resource: str | os.PathLike) -> str | os.PathLike:
    """
    Resolves a CDN URL given a file path that is relative to
    an extension dist directory.
    """
    for p, cdn in EXTENSION_CDN.items():
        if _is_subpath(resource, p):
            resource = str(resource).replace(os.path.abspath(p), cdn).replace(os.path.sep, '/')
            break
    return resource

def resolve_stylesheet(cls, stylesheet: str, attribute: str | None = None):
    """
    Resolves a stylesheet definition, e.g. originating on a component
    Reactive._stylesheets or a Design.modifiers attribute. Stylesheets
    may be defined as one of the following:

    - Absolute URL defined with http(s) protocol
    - A path relative to the component
    - A raw css string

    Parameters
    ----------
    cls: type | object
        Object or class defining the stylesheet
    stylesheet: str
        The stylesheet definition
    """
    stylesheet = os.fspath(stylesheet)
    if stylesheet.startswith('http') or not (attribute and _is_file_path(stylesheet) and (custom_path:= resolve_custom_path(cls, stylesheet))):
        return stylesheet
    if not state._is_pyodide and state.curdoc and state.curdoc.session_context:
        stylesheet = component_resource_path(cls, attribute, stylesheet)
        stylesheet = add_version_suffix(stylesheet)
    else:
        try:
            stylesheet = custom_path.read_text(encoding='utf-8')
        except FileNotFoundError as e:
            raise ResourceNotFoundError(
                f"Component stylesheet not found: {custom_path}",
                kind='file',
                relpath=str(custom_path),
                component=cls,
                attr=attribute,
            ) from e
    return stylesheet

def patch_model_css(root: Model, dist_url: str):
    """
    Temporary patch for Model.css property used by Panel to provide
    stylesheets for components.

    ALERT: Should find better solution before official Bokeh 3.x compatible release
    """
    # Patch model CSS properties
    doc = root.document
    if doc:
        held = doc.callbacks.hold_value
        events = list(doc.callbacks._held_events)
        doc.hold()
    for stylesheet in root.select({'type': ImportedStyleSheet}):
        patch_stylesheet(stylesheet, dist_url)
    if doc:
        doc.callbacks._held_events = events
        if held or not state._loaded.get(doc):
            doc.callbacks._hold = held
        else:
            doc.unhold()

def global_css(name: str) -> str:
    if RESOURCE_MODE == 'server':
        return f'static/extensions/panel/css/{name}.css'
    return f'{CDN_DIST}css/{name}.css'

def bundled_files(model: Model, file_type: str = 'javascript') -> list[str]:
    name = model.__name__.lower()
    raw_files = getattr(model, f"__{file_type}_raw__", [])
    for cls in model.__mro__[1:]:
        cls_files = getattr(cls, f"__{file_type}_raw__", [])
        if raw_files is cls_files:
            name = cls.__name__.lower()
    paths = get_resource_paths()
    bdir = paths.bundle_dir / name
    shared = list((JS_URLS if file_type == 'javascript' else CSS_URLS).values())
    files: list[str] = []
    npm_cdn_prefixes = (config.npm_cdn, 'https://cdn.jsdelivr.net/npm', 'https://unpkg.com')
    for url in raw_files:
        if url.startswith(CDN_DIST):
            filepath = url.replace(f'{CDN_DIST}bundled/', '')
        elif url.startswith(npm_cdn_prefixes):
            for prefix in npm_cdn_prefixes:
                if url.startswith(prefix):
                    filepath = url.replace(prefix, '')[1:]
                    break
        else:
            filepath = url_path(url)
        test_filepath = filepath.split('?')[0]
        if url in shared:
            prefixed = filepath
            test_path = paths.bundle_dir / test_filepath
        elif not test_filepath.replace('/', '').startswith(f'{name}/'):
            prefixed = f'{name}/{test_filepath}'
            test_path = bdir / test_filepath
        else:
            prefixed = test_filepath
            test_path = paths.bundle_dir / test_filepath
        try:
            test_ok = test_path.is_file()
        except OSError:
            test_ok = False
        if test_ok:
            if RESOURCE_MODE == 'server':
                files.append(f'static/extensions/panel/bundled/{prefixed}')
            elif filepath == test_filepath:
                files.append(f'{CDN_DIST}bundled/{prefixed}')
            else:
                files.append(url)
        elif RESOURCE_MODE == 'server':
            local_url = f'static/extensions/panel/bundled/{prefixed}'
            logger.warning(
                "Bundled %s file %s not found locally at %s. "
                "Using local server URL %s anyway (file may be populated at runtime). %s",
                file_type, prefixed, test_path, local_url,
                "Run `panel build` to populate dist/." if paths.install_mode == 'editable' else "",
            )
            files.append(local_url)
        else:
            cdn_url = f'{CDN_DIST}bundled/{prefixed}'
            logger.warning(
                "Bundled %s file %s not found locally at %s. "
                "Falling back to CDN URL %s. %s",
                file_type, prefixed, test_path, cdn_url,
                "Run `panel build` to populate dist/." if paths.install_mode == 'editable' else "",
            )
            files.append(cdn_url)
    return files

def _panel_use_mathjax(roots) -> bool:
    """Whether any model in roots is a Panel HTML model (may need MathJax)."""
    from ..models.markup import HTML as PanelHTML
    return _any(roots, lambda obj: isinstance(obj, PanelHTML))


def bundle_resources(
    roots,
    resources: BkResources,
    notebook: bool = False,
    reloading: bool = False,
    enable_mathjax: bool | t.Literal['auto'] = 'auto'
):
    from ..config import panel_extension as ext
    global RESOURCE_MODE
    if not isinstance(resources, Resources):
        resources = Resources.from_bokeh(resources, notebook=notebook)
    js_resources = css_resources = resources
    RESOURCE_MODE = mode = js_resources.mode if resources is not None else "inline"

    js_files = []
    js_raw = []
    css_files = []
    css_raw = []

    if isinstance(enable_mathjax, bool):
        use_mathjax = enable_mathjax
    elif roots:
        use_mathjax = (
            _use_mathjax(roots) or
            _panel_use_mathjax(roots) or
            'mathjax' in ext._loaded_extensions
        )
    else:
        use_mathjax = 'mathjax' in ext._loaded_extensions

    if js_resources:
        js_resources = js_resources.clone()
        if not use_mathjax and "bokeh-mathjax" in js_resources.components:
            js_resources.components.remove("bokeh-mathjax")
        if reloading:
            js_resources.components.clear()

        js_files.extend(js_resources.js_files)
        js_raw.extend(js_resources.js_raw)

    css_files.extend(css_resources.css_files)
    css_raw.extend(css_resources.css_raw)

    extensions = _bundle_extensions(None, js_resources)
    if reloading:
        extensions = [
            ext for ext in extensions if not (ext.cdn_url is not None and str(ext.cdn_url).startswith('https://unpkg.com/@holoviz/panel@'))
        ]

    extra_js = []
    if mode == "inline":
        js_raw.extend([ Resources._inline(bundle.artifact_path) for bundle in extensions ])
    elif mode == "server":
        for bundle in extensions:
            server_url = str(bundle.server_url)
            if resources.root_url and not resources.absolute:
                server_url = server_url.replace(resources.root_url, '', 1)
                if state.rel_path:
                    server_url = f'{state.rel_path}/{server_url}'
            js_files.append(server_url)
    elif mode == "cdn":
        for bundle in extensions:
            if bundle.cdn_url is not None:
                extra_js.append(str(bundle.cdn_url))
            else:
                js_raw.append(Resources._inline(bundle.artifact_path))
    else:
        extra_js.extend([str(bundle.artifact_path) for bundle in extensions])
    js_files += resources.adjust_paths(extra_js)

    extension = bundle_models(None)
    if extension is not None:
        js_raw.append(extension)

    hashes = js_resources.hashes if js_resources else {}

    return Bundle(
        css_files=[URL(css_file) for css_file in css_files],
        css_raw=css_raw,
        hashes=hashes,
        js_files=[URL(js_file) for js_file in js_files],
        js_raw=js_raw,
        js_module_exports=resources.js_module_exports,
        js_modules=resources.js_modules,
        notebook=notebook,
    )


class ResourceComponent:
    """
    Mix-in class for components that define a set of resources
    that have to be resolved.
    """

    _resources: t.ClassVar[ResourcesType] = {
        'css': {},
        'font': {},
        'js': {},
        'js_modules': {},
        'raw_css': [],
    }

    @classmethod
    def _resolve_resource(
        cls,
        resource_type: str,
        resource: str,
        cdn: bool = False
    ) -> str:
        try:
            resolved: ResolvedResource = resolve_resource(
                cls, resource_type, resource, cdn=cdn
            )
        except ResourceNotFoundError:
            raise FileNotFoundError(f'Could not resolve resource {resource!r}')
        return resolved.value

    def resolve_resources(
        self,
        cdn: bool | t.Literal['auto'] = 'auto',
        extras: dict[str, dict[str, str]] | None = None
    ) -> ResourcesType:
        """
        Resolves the resources required for this component.

        Parameters
        ----------
        cdn: bool | Literal['auto']
            Whether to load resources from CDN or local server. If set
            to 'auto' value will be automatically determine based on
            global settings.
        extras: dict[str, dict[str, str]] | None
            Additional resources to add to the bundle. Valid resource
            types include js, js_modules and css.

        Returns
        -------
        Dictionary containing JS and CSS resources.
        """
        cls = type(self)
        resources: ResourcesType = {}
        for rt, res in self._resources.items():
            if not isinstance(res, dict):
                continue
            if rt == 'font':
                rt = 'css'
            res = {
                name: url if isurl(url) else f'{cls.__name__.lower()}/{url}'
                for name, url in res.items()
            }
            if rt in resources:
                resources[rt] = dict(resources[rt], **res)  # type: ignore
            else:
                resources[rt] = res  # type: ignore

        resource_types: ResourcesType = {
            'js': {},
            'js_modules': {},
            'css': {},
            'raw_css': []
        }

        cdn = use_cdn() if cdn == 'auto' else cdn
        for resource_type in resource_types:
            if resource_type not in resources or resource_type == 'raw_css':  # type: ignore
                continue
            resource_files = resource_types[resource_type]  # type: ignore
            for rname, resource in resources[resource_type].items():  # type: ignore
                resolved_resource = self._resolve_resource(
                    resource_type, resource, cdn=cdn
                )
                if resolved_resource:
                    resource_files[rname] = resolved_resource  # type: ignore

        vsuffix = _version_suffix()
        dist_path = get_dist_path(cdn=cdn)
        for resource_type, extra_resources in (extras or {}).items():
            resource_files = resource_types[resource_type]  # type: ignore
            for name, res in extra_resources.items():
                if not cdn:
                    res = res.replace(CDN_DIST, dist_path)
                    res = add_version_suffix(res)
                resource_files[name] = res

        return resource_types


class Resources(BkResources):

    def __init__(self, *args, absolute=False, notebook=False, **kwargs):
        self.absolute = absolute
        self.notebook = notebook
        super().__init__(*args, **kwargs)

    @classmethod
    def from_bokeh(cls, bkr, absolute=False, notebook=False):
        kwargs = {}
        if bkr.mode.startswith("server"):
            kwargs['root_url'] = bkr.root_url

        components = bkr.components if hasattr(bkr, 'components_for') else bkr._components
        return cls(
            mode=bkr.mode, version=bkr.version, minified=bkr.minified,
            log_level=bkr.log_level, notebook=notebook,
            path_versioner=bkr.path_versioner,
            components=components, base_dir=bkr.base_dir,
            root_dir=bkr.root_dir, absolute=absolute, **kwargs
        )

    def _collect_external_resources(self, resource_attr: ResourceAttr) -> list[str]:
        """ Collect external resources set on resource_attr attribute of all models."""
        external_resources: list[str] = []

        if state._extensions is not None:
            external_modules = {
                module: ext for ext, module in extension._imports.items()
            }
        else:
            external_modules = None

        for _, cls in sorted(Model.model_class_reverse_map.items(), key=lambda arg: arg[0]):
            if external_modules is not None and cls.__module__ in external_modules:
                if external_modules[cls.__module__] not in state._extensions:
                    continue
            external: list[str] | str | None = getattr(cls, resource_attr, None)

            if isinstance(external, str):
                if external not in external_resources:
                    external_resources.append(external)
            elif isinstance(external, list):
                for e in external:
                    if e not in external_resources:
                        external_resources.append(e)

        return external_resources

    def _server_urls(self) -> Urls:
        return _get_server_urls(
            self.root_url if self.absolute else '',
            False if self.dev else self.minified,
            self.path_versioner
        )

    def extra_resources(self, resources, resource_type):
        """
        Adds resources for ReactiveHTML components.
        """
        from ..reactive import ReactiveCustomBase
        for model in _descendents(ReactiveCustomBase, concrete=True):
            cls_files = getattr(model, resource_type, None)
            if not (cls_files and model._loaded()):
                continue
            for cls in model.__mro__[1:]:
                supcls_files = getattr(cls, resource_type, [])
                if supcls_files == cls_files:
                    model = cls
            for resource in getattr(model, resource_type, []):
                if isinstance(resource, pathlib.PurePath):
                    continue
                if self.mode == 'cdn':
                    resource = resolve_resource_cdn(resource)
                if state.rel_path:
                    resource = resource.removeprefix(state.rel_path+'/')
                if not isurl(resource) and not resource.lstrip('./').startswith('static/extensions'):
                    resource = component_resource_path(model, resource_type, resource)
                if resource not in resources:
                    resources.append(resource)

    def adjust_paths(self, resources):
        """
        Computes relative and absolute paths for resources.
        """
        new_resources = []
        cdn_base = f'{config.npm_cdn}/@holoviz/panel@{JS_VERSION}/dist/'
        for resource in resources:
            if not isinstance(resource, str):
                resource = str(resource)
            resource = resource.replace('https://unpkg.com', config.npm_cdn)
            if resource.startswith(cdn_base):
                resource = resource.replace(cdn_base, CDN_DIST)
            if self.mode == 'cdn':
                resource = resolve_resource_cdn(resource)
            if self.mode == 'server':
                resource = resource.replace(CDN_DIST, LOCAL_DIST)
            if resource.startswith((state.base_url, "static/")):
                if resource.startswith(state.base_url):
                    resource = resource[len(state.base_url):]
                if state.rel_path:
                    resource = f'{state.rel_path}/{resource}'
                elif self.absolute and self.mode == 'server':
                    resource = f'{self.root_url}{resource}'
            if resource.endswith('.css') and not resource.startswith(('http:', 'https:')):
                resource = add_version_suffix(resource)
            new_resources.append(resource)
        return new_resources

    def clone(self, *, components=None) -> Resources:
        """
        Make a clone of a resources instance allowing to override its components.
        """
        return Resources(
            mode=self.mode,
            version=self.version,
            root_dir=self.root_dir,
            dev=self.dev,
            minified=self.minified,
            log_level=self.log_level,
            root_url=self._root_url,
            path_versioner=self.path_versioner,
            components=components if components is not None else list(self.components),
            base_dir=self.base_dir,
            notebook=self.notebook,
            absolute=self.absolute
        )

    @property
    def dist_dir(self):
        if self.notebook and self.mode == 'server':
            dist_dir = '/panel-preview/static/extensions/panel/'
        elif self.mode == 'server':
            if state.rel_path:
                dist_dir = f'{state.rel_path}/{LOCAL_DIST}'
            else:
                dist_dir = LOCAL_DIST
            if self.absolute:
                dist_dir = f'{self.root_url}{dist_dir}'
        else:
            dist_dir = CDN_DIST
        return dist_dir

    @property
    def css_files(self):
        from ..config import config

        files = super().css_files
        self.extra_resources(files, '__css__')
        self.extra_resources(files, '_bundle_css')
        if config.notifications and state.notifications:
            files += state.notifications._stylesheets
        if self.mode == 'inline':
            # In inline mode, keep CDN URLs only for dist files that are
            # missing locally (those that exist will be inlined in css_raw).
            css_files = self.adjust_paths([
                css for css in files
                if not is_cdn_url(css) or not dist_file_exists(css.replace(CDN_DIST, ''))
            ])
        else:
            css_files = self.adjust_paths(files)
        if config.design:
            css_files += list(config.design._resources.get('font', {}).values())
        for cssf in config.css_files:
            if os.path.isfile(cssf) or cssf in files:
                continue
            css_files.append(cssf)
        return css_files

    def _inline_read_dist_file(self, relative_path: str) -> str | None:
        """
        Read a dist file for inline mode.  Returns the text content if
        the file is available locally, or ``None`` (with a helpful
        warning logged) when the file is missing.  Callers should skip
        inlining when ``None`` is returned and keep the CDN URL as an
        external resource instead.
        """
        return read_dist_text(relative_path)

    @property
    def css_raw(self):
        from ..config import config
        raw = super().css_raw

        # Inline local dist resources — skip files missing locally;
        # those will be kept as external CDN URLs in css_files.
        css_files = self._collect_external_resources("__css__")
        self.extra_resources(css_files, '__css__')
        if self.mode.lower() not in ('server', 'cdn'):
            inlined = []
            for css in css_files:
                if not is_cdn_url(css):
                    continue
                content = self._inline_read_dist_file(css.replace(CDN_DIST, ''))
                if content is not None:
                    inlined.append(content)
            raw += inlined

        # Add local CSS files
        for cssf in config.css_files:
            if not os.path.isfile(cssf):
                continue
            css_txt = process_raw_css([Path(cssf).read_text(encoding='utf-8')])[0]
            if css_txt not in raw:
                raw.append(css_txt)

        # Add loading spinner
        if config.global_loading_spinner:
            loading_base = self._inline_read_dist_file("css/loading.css")
            if loading_base is not None:
                loading_base = loading_base.replace('../assets', self.dist_dir + 'assets')
                raw.append(loading_base)
            raw.append(loading_css(
                config.loading_spinner, config.loading_color, config.loading_max_height
            ))
        return raw + process_raw_css(config.raw_css) + process_raw_css(config.global_css)

    @property
    def js_files(self):
        from ..config import config

        # Gather JS files
        with set_resource_mode(self.mode):
            files = super().js_files
            self.extra_resources(files, '__javascript__')
        files += [js for js in config.js_files.values()]
        if config.design:
            design_js = config.design().resolve_resources(
                cdn=self.notebook or 'auto', include_theme=False
            )['js'].values()
            files += [res for res in design_js if res not in files]

        # Filter and adjust JS file urls — in inline mode, only strip CDN
        # URLs whose corresponding local dist file is present (those will
        # be inlined in js_raw).  Keep CDN URLs for files missing locally
        # so they still load as external resources.
        if self.mode == 'inline':
            js_files = self.adjust_paths([
                js for js in files
                if not is_cdn_url(js) or not dist_file_exists(js.replace(CDN_DIST, ''))
            ])
        else:
            js_files = self.adjust_paths(files)

        # Load requirejs last to avoid interfering with other libraries
        require_index = [i for i, jsf in enumerate(js_files) if 'require' in jsf]
        if require_index:
            requirejs = js_files.pop(require_index[0])
            js_files.append(requirejs)

        return js_files

    @property
    def js_modules(self):
        from ..config import config
        from ..reactive import ReactiveCustomBase

        modules = list(config.js_modules.values())
        for model in Model.model_class_reverse_map.values():
            if not hasattr(model, '__javascript_modules__'):
                continue
            for module in model.__javascript_modules__:
                if module not in modules:
                    modules.append(module)

        self.extra_resources(modules, '__javascript_modules__')
        if config.design:
            design_resources = config.design().resolve_resources(
                cdn=self.notebook or 'auto', include_theme=False
            )
            modules += [
                res for res in design_resources['js_modules'].values()
                if res not in modules
            ]

        for model in _descendents(ReactiveCustomBase, concrete=True):
            if not (getattr(model, '__javascript_modules__', None) and model._loaded()):
                continue
            for js_module in model.__javascript_modules__:
                if state.rel_path:
                    js_module = js_module.removeprefix(state.rel_path+'/')
                if not isurl(js_module) and not js_module.startswith('static/extensions'):
                    js_module = component_resource_path(model, '__javascript_modules__', js_module)
                if js_module not in modules:
                    modules.append(js_module)

        return self.adjust_paths(modules)

    @property
    def js_module_exports(self):
        modules = {}
        for model in Model.model_class_reverse_map.values():
            if hasattr(model, '__javascript_module_exports__'):
                modules.update(dict(zip(model.__javascript_module_exports__, model.__javascript_modules__)))
        return dict(zip(modules, self.adjust_paths(modules.values())))

    @property
    def js_raw(self):
        raw_js = super().js_raw
        if not self.mode == 'inline':
            return raw_js

        # Inline local dist resources — skip files missing locally will stay in js_files
        js_files = self._collect_external_resources("__javascript__")
        self.extra_resources(js_files, '__javascript__')
        for js in js_files:
            if not is_cdn_url(js):
                continue
            content = self._inline_read_dist_file(js.replace(CDN_DIST, ''))
            if content is not None:
                raw_js.append(content)

        # Inline config.js_files
        from ..config import config
        raw_js += [
            Path(js).read_text(encoding='utf-8') for js in config.js_files.values()
            if os.path.isfile(js)
        ]

        # Inline config.design JS resources
        if config.design:
            design_js = config.design().resolve_resources(
                cdn=True, include_theme=False
            )['js'].values()
            for js in design_js:
                if not is_cdn_url(js):
                    continue
                content = self._inline_read_dist_file(js.replace(CDN_DIST, ''))
                if content is not None:
                    raw_js.append(content)
        return raw_js

    @property
    def render_js(self):
        return JS_RESOURCES.render(
            js_raw=self.js_raw, js_files=self.js_files,
            js_modules=self.js_modules, hashes=self.hashes,
            js_module_exports=self.js_module_exports
        )


class Bundle(BkBundle):

    def __init__(self, notebook=False, **kwargs):
        self.js_modules = kwargs.pop("js_modules", [])
        self.js_module_exports = kwargs.pop("js_module_exports", {})
        self.notebook = notebook
        super().__init__(**kwargs)

    @classmethod
    def from_bokeh(cls, bk_bundle, notebook=False):
        return cls(
            notebook=notebook,
            js_files=bk_bundle.js_files,
            js_raw=bk_bundle.js_raw,
            css_files=bk_bundle.css_files,
            css_raw=bk_bundle.css_raw,
            hashes=bk_bundle.hashes,
        )

    def _render_css(self) -> str:
        return BkCSS_RESOURCES.render(
            css_files=self.css_files,
            css_raw=self.css_raw
        )

    def _render_js(self):
        return JS_RESOURCES.render(
            js_raw=self.js_raw,
            js_files=self.js_files,
            js_modules=self.js_modules,
            js_module_exports=self.js_module_exports,
            hashes=self.hashes
        )
