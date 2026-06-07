from __future__ import annotations

import base64
import concurrent.futures
import dataclasses
import json
import os
import pathlib
import re
import typing as t
import uuid

from html import escape
from urllib.parse import urlparse
from zipfile import ZipFile

import bokeh

from bokeh.application.handlers.code import CodeHandler
from bokeh.core.json_encoder import serialize_json
from bokeh.core.templates import FILE, MACROS, get_env
from bokeh.document import Document
from bokeh.embed.elements import script_for_render_items
from bokeh.embed.util import RenderItem, standalone_docs_json_and_render_items
from bokeh.embed.wrappers import wrap_in_script_tag
from bokeh.util.serialization import make_id
from packaging.requirements import Requirement

from .. import __version__, config
from ..util import base_version
from .application import Application, build_single_handler_application
from .document import MockSessionContext
from .loading import LOADING_INDICATOR_CSS_CLASS
from .mime_render import find_requirements
from .resources import (
    BASE_TEMPLATE, CDN_DIST, CDN_ROOT, DIST_DIR, INDEX_TEMPLATE, Resources,
    _env as _pn_env, bundle_resources, loading_css, set_resource_mode,
)
from .state import set_curdoc, state

if t.TYPE_CHECKING:
    from collections.abc import Sequence

PWA_MANIFEST_TEMPLATE = _pn_env.get_template('site.webmanifest')
SERVICE_WORKER_TEMPLATE = _pn_env.get_template('serviceWorker.js')
WEB_WORKER_TEMPLATE = _pn_env.get_template('pyodide_worker.js')
WORKER_HANDLER_TEMPLATE = _pn_env.get_template('pyodide_handler.js')

PANEL_ROOT = pathlib.Path(__file__).parent.parent
BOKEH_VERSION = base_version(bokeh.__version__)
PY_VERSION = base_version(__version__)
PYODIDE_VERSION = 'v0.29.3'
PYSCRIPT_VERSION = '2026.2.1'
WHL_PATH = DIST_DIR / 'wheels'
PANEL_LOCAL_WHL = WHL_PATH / f'panel-{__version__.replace("-dirty", "")}-py3-none-any.whl'
BOKEH_LOCAL_WHL = WHL_PATH / f'bokeh-{BOKEH_VERSION}-py3-none-any.whl'
PANEL_CDN_WHL = f'{CDN_DIST}wheels/panel-{PY_VERSION}-py3-none-any.whl'
BOKEH_CDN_WHL = f'{CDN_ROOT}wheels/bokeh-{BOKEH_VERSION}-py3-none-any.whl'
PYODIDE_URL = f'https://cdn.jsdelivr.net/pyodide/{PYODIDE_VERSION}/full/pyodide.js'
PYODIDE_PYC_URL = f'https://cdn.jsdelivr.net/pyodide/{PYODIDE_VERSION}/pyc/pyodide.js'
PYSCRIPT_CSS = f'<link rel="stylesheet" href="https://pyscript.net/releases/{PYSCRIPT_VERSION}/core.css" />'
PYSCRIPT_CSS_OVERRIDES = f'<link rel="stylesheet" href="{CDN_DIST}css/pyscript.css" />'
PYSCRIPT_JS = f'<script type="module" src="https://pyscript.net/releases/{PYSCRIPT_VERSION}/core.js" defer></script>'
PYODIDE_JS = f'<script src="{PYODIDE_URL}" defer></script>'
PYODIDE_PYC_JS = f'<script src="{PYODIDE_PYC_URL}" defer></script>'
LOCAL_PREFIX = './'

MINIMUM_VERSIONS: dict[str, str] = {}

ICON_DIR = DIST_DIR / 'images'
PWA_IMAGES = [
    ICON_DIR / 'favicon.ico',
    ICON_DIR / 'icon-vector.svg',
    ICON_DIR / 'icon-32x32.png',
    ICON_DIR / 'icon-192x192.png',
    ICON_DIR / 'icon-512x512.png',
    ICON_DIR / 'apple-touch-icon.png',
    ICON_DIR / 'index_background.png'
]

Runtimes = t.Literal['pyodide', 'pyscript', 'pyodide-worker', 'pyscript-worker']

_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']')
_LINK_HREF_RE = re.compile(r'<link[^>]+href=["\']([^"\']+)["\']')


def _extract_urls_from_tags(tag_strings: list[str] | tuple[str, ...]) -> list[str]:
    """Extract resource URLs from HTML <script> and <link> tags."""
    urls: list[str] = []
    for tag in tag_strings:
        if not isinstance(tag, str):
            continue
        for m in _SCRIPT_SRC_RE.finditer(tag):
            url = m.group(1)
            if url and url not in urls:
                urls.append(url)
        for m in _LINK_HREF_RE.finditer(tag):
            url = m.group(1)
            if url and url not in urls:
                urls.append(url)
    return urls


def _collect_bundle_urls(bundle, extra_js_tags: list[str], extra_css_tags: list[str]) -> list[str]:
    """
    Collect all external JS/CSS URLs from a resource Bundle and extra tag strings.
    Returns a deduplicated list of URLs suitable for service worker pre-caching.
    """
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: str):
        if not u or u in seen or u.startswith('data:'):
            return
        seen.add(u)
        urls.append(u)

    if bundle is not None:
        for jsf in getattr(bundle, 'js_files', []) or []:
            _add(str(jsf))
        for jsf in getattr(bundle, 'js_modules', []) or []:
            _add(str(jsf))
        for cssf in getattr(bundle, 'css_files', []) or []:
            _add(str(cssf))

    for url in _extract_urls_from_tags(extra_js_tags):
        _add(url)
    for url in _extract_urls_from_tags(extra_css_tags):
        _add(url)

    return urls


def _collect_document_stylesheet_urls(document) -> list[str]:
    """
    Collect all external stylesheet URLs from ImportedStyleSheet objects
    attached to models in the document. This catches design/theme stylesheets
    that are applied at the model level rather than in the global bundle.
    """
    from bokeh.models import ImportedStyleSheet
    urls: list[str] = []
    seen: set[str] = set()
    for model in document.models:
        stylesheets = getattr(model, 'stylesheets', None)
        if not stylesheets:
            continue
        for ss in stylesheets:
            if isinstance(ss, ImportedStyleSheet) and ss.url:
                url = ss.url.split('?')[0]
                if url and url not in seen and not url.startswith('data:'):
                    seen.add(url)
                    urls.append(url)
    return urls


def _parse_extension_calls_from_source(source: str) -> list[str]:
    """
    Parse Python source code and extract extension names passed to
    ``pn.extension(...)`` or ``panel.extension(...)`` calls.
    Handles positional string args and some keyword-argument usages.
    """
    names: list[str] = []
    seen: set[str] = set()
    patterns = [
        re.compile(r'''(?:pn|panel)\s*\.\s*extension\s*\(\s*((?:['"][^'"]+['"]\s*,?\s*)+)'''),
        re.compile(r'''(?:pn|panel)\s*\.\s*extension\s*\(\s*\[((?:\s*['"][^'"]+['"]\s*,?\s*)+)\]'''),
    ]
    str_pattern = re.compile(r'''['"]([^'"]+)['"]''')
    for pat in patterns:
        for m in pat.finditer(source):
            for sm in str_pattern.finditer(m.group(1)):
                name = sm.group(1)
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
    return names


def _detect_extensions_from_document(document) -> list[str]:
    """
    Scan all models in a document and infer which Panel extensions are used.
    Maps model class module paths to extension names using the panel_extension
    import registry plus the ReactiveHTML extension-name registry.
    """
    from ..config import panel_extension
    from ..reactive import ReactiveHTML
    module_to_ext = {v: k for k, v in panel_extension._imports.items()}
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str):
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for model in document.models:
        module = type(model).__module__
        ext = module_to_ext.get(module)
        if ext:
            _add(ext)
        if isinstance(model, ReactiveHTML) and getattr(model, '_extension_name', None):
            _add(model._extension_name)

    for ext in getattr(state, '_extensions', []) or []:
        _add(ext)
    state_exts = getattr(state, '_extensions_', {}) or {}
    for exts in state_exts.values():
        for e in exts:
            _add(e)
    for e in panel_extension._loaded_extensions:
        _add(e)
    return names


def _resolve_extension_resource_urls(extension_names: list[str]) -> dict[str, list[str]]:
    """
    Given a list of Panel extension names, resolve the JS and CSS URLs
    that each extension requires by importing its model module and using
    the bundled_files helper plus the Resources class.
    Returns dict with keys 'js', 'css', 'js_modules'.
    """
    from bokeh.model import Model
    from ..config import panel_extension
    from ..io.resources import bundled_files

    result: dict[str, list[str]] = {'js': [], 'css': [], 'js_modules': []}
    seen_js: set[str] = set()
    seen_css: set[str] = set()

    def _add_js(url: str):
        if url and url not in seen_js and not url.startswith('data:'):
            seen_js.add(url)
            result['js'].append(url)

    def _add_css(url: str):
        if url and url not in seen_css and not url.startswith('data:'):
            seen_css.add(url)
            result['css'].append(url)

    for ext in extension_names:
        module_path = panel_extension._imports.get(ext)
        if not module_path:
            continue
        try:
            import importlib
            module = importlib.import_module(module_path)
        except Exception:
            continue
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Model) and obj is not Model:
                try:
                    for url in bundled_files(obj, 'javascript'):
                        _add_js(url)
                except Exception:
                    pass
                try:
                    for url in bundled_files(obj, 'css'):
                        _add_css(url)
                except Exception:
                    pass
                ext_name = getattr(obj, '_extension_name', None)
                if ext_name:
                    for attr in ('__javascript__',):
                        urls = getattr(obj, attr, None)
                        if isinstance(urls, (list, tuple)):
                            for u in urls:
                                _add_js(str(u))
                    for attr in ('__css__',):
                        urls = getattr(obj, attr, None)
                        if isinstance(urls, (list, tuple)):
                            for u in urls:
                                _add_css(str(u))
    return result


def _resolve_design_theme_resource_urls() -> dict[str, list[str]]:
    """Resolve Design/Theme CSS and JS URLs from the global config."""
    result: dict[str, list[str]] = {'js': [], 'css': [], 'js_modules': []}
    if config.design:
        try:
            res = config.design().resolve_resources(cdn=True, include_theme=True)
            for rtype in ('js', 'css', 'js_modules'):
                for url in res.get(rtype, {}).values():
                    if url and not url.startswith('data:') and url not in result[rtype]:
                        result[rtype].append(url)
        except Exception:
            pass
        font_css = list(config.design._resources.get('font', {}).values())
        for url in font_css:
            if url and not url.startswith('data:') and url not in result['css']:
                result['css'].append(url)
    for url in config.css_files:
        if not os.path.isfile(url) and url not in result['css']:
            result['css'].append(url)
    for url in config.js_files.values() if hasattr(config.js_files, 'values') else config.js_files:
        if isinstance(url, str) and not url.startswith('data:') and url not in result['js']:
            result['js'].append(url)
    return result


def _resolve_runtime_resource_urls(
    runtime: Runtimes,
    compiled: bool = False,
) -> dict[str, list[str]]:
    """Resolve Pyodide/PyScript JS and CSS URLs."""
    result: dict[str, list[str]] = {'js': [], 'css': [], 'js_modules': []}
    if runtime.startswith('pyscript'):
        result['js'].append(f'https://pyscript.net/releases/{PYSCRIPT_VERSION}/core.js')
        result['css'].append(f'https://pyscript.net/releases/{PYSCRIPT_VERSION}/core.css')
        result['css'].append(f'{CDN_DIST}css/pyscript.css')
    else:
        if compiled:
            result['js'].append(PYODIDE_PYC_URL)
        else:
            result['js'].append(PYODIDE_URL)
    result['css'].append(f'{CDN_DIST}css/loading.css')
    return result


def _safe_filename(url: str) -> str:
    """Convert a URL into a safe flat filename for local storage."""
    parsed = urlparse(url)
    path = parsed.path
    if not path:
        path = parsed.netloc
    basename = os.path.basename(path) or 'resource'
    base, ext = os.path.splitext(basename)
    if not ext:
        ext = '.bin'
    cleaned_base = re.sub(r'[^A-Za-z0-9._-]', '_', base)[:80]
    url_hash = hex(abs(hash(url)))[2:10]
    return f'{cleaned_base}_{url_hash}{ext}'


def _download_or_copy_url(url: str, dest_path: pathlib.Path) -> bool:
    """
    Try to get the content for a URL from (in order):
    1. A local panel bundled file (if it matches CDN_DIST path)
    2. A local file path
    3. A remote HTTP download
    Returns True on success.
    """
    from ..io.resources import BUNDLE_DIR

    try:
        if url.startswith(CDN_DIST):
            rel = url[len(CDN_DIST):].split('?')[0]
            candidate = DIST_DIR / rel
            if candidate.is_file():
                dest_path.write_bytes(candidate.read_bytes())
                return True
            bundled_rel = rel.replace('bundled/', '', 1) if 'bundled/' in rel else rel
            candidate2 = BUNDLE_DIR / bundled_rel
            if candidate2.is_file():
                dest_path.write_bytes(candidate2.read_bytes())
                return True

        parsed = urlparse(url)
        if parsed.scheme in ('', 'file'):
            local_path = pathlib.Path(parsed.path or url).expanduser().resolve()
            if local_path.is_file():
                dest_path.write_bytes(local_path.read_bytes())
                return True

        if parsed.scheme in ('http', 'https'):
            import urllib.request
            import ssl
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, headers={'User-Agent': 'panel-convert/1.0'})
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                if resp.status == 200:
                    dest_path.write_bytes(resp.read())
                    return True
    except Exception:
        pass
    return False


def build_local_assets(
    resource_urls: list[str],
    dest_dir: str | os.PathLike,
    assets_subdir: str = 'assets',
    verbose: bool = True,
) -> tuple[dict[str, str], pathlib.Path, list[str]]:
    """
    Download / copy a list of resource URLs into a local assets directory.

    Parameters
    ----------
    resource_urls : list[str]
        All URLs (CDN, local file paths, http, https) to localize.
    dest_dir : path-like
        Output directory for the converted app.
    assets_subdir : str
        Sub-directory name under dest_dir to store assets.
    verbose : bool
        Print progress.

    Returns
    -------
    (url_mapping, assets_path, local_asset_paths)
        url_mapping: dict mapping original URL -> './assets/filename' (relative for HTML)
        assets_path: absolute path to the assets directory
        local_asset_paths: list of relative paths ('.\\/assets/...') for service worker caching
    """
    dest_path = pathlib.Path(dest_dir)
    assets_path = dest_path / assets_subdir
    assets_path.mkdir(parents=True, exist_ok=True)

    url_mapping: dict[str, str] = {}
    local_rel_paths: list[str] = []
    seen_local: set[str] = set()

    for url in resource_urls:
        if not url or url.startswith('data:'):
            continue
        if url in url_mapping:
            continue
        filename = _safe_filename(url)
        target = assets_path / filename
        if _download_or_copy_url(url, target):
            rel = f'./{assets_subdir}/{filename}'
            url_mapping[url] = rel
            if rel not in seen_local:
                seen_local.add(rel)
                local_rel_paths.append(rel)
            if verbose:
                print(f'  [asset] localized: {os.path.basename(url)} -> {rel}')
        elif verbose:
            print(f'  [asset] WARNING: could not localize {url}')
    return url_mapping, assets_path, local_rel_paths


_URL_REWRITE_PATTERNS: list[tuple[re.Pattern, str]] = []


def _build_rewrite_patterns(mapping: dict[str, str]) -> None:
    """Build and cache regex patterns for URL rewriting in HTML/JS content."""
    global _URL_REWRITE_PATTERNS
    sorted_keys = sorted(mapping.keys(), key=len, reverse=True)
    patterns: list[tuple[re.Pattern, str]] = []
    for key in sorted_keys:
        escaped = re.escape(key)
        pat = re.compile(r'(?<=[\(\'"=,\s])' + escaped + r'(?=[\?\s\'"\),>])')
        patterns.append((pat, mapping[key]))
        without_qs = key.split('?')[0]
        if without_qs != key and without_qs in mapping:
            continue
        if without_qs != key:
            pat2 = re.compile(r'(?<=[\(\'"=,\s])' + re.escape(without_qs) + r'(?=[\?\s\'"\),>])')
            patterns.append((pat2, mapping[key]))
    _URL_REWRITE_PATTERNS = patterns


def rewrite_local_urls(content: str, url_mapping: dict[str, str]) -> str:
    """Rewrite all known CDN/remote URLs in content to their local asset paths."""
    if not url_mapping:
        return content
    _build_rewrite_patterns(url_mapping)
    for pat, replacement in _URL_REWRITE_PATTERNS:
        content = pat.sub(replacement, content)
    for original, local in url_mapping.items():
        for variant in (original, original.split('?')[0]):
            if variant in content and variant not in (original, original.split('?')[0] if original != original.split('?')[0] else ''):
                continue
            if variant in content:
                content = content.replace(variant, local)
    return content

PRE = """
import asyncio

from panel.io.pyodide import init_doc, write_doc

init_doc()
"""

POST = """
await write_doc()"""

POST_PYSCRIPT = """
asyncio.ensure_future(write_doc());"""

PYODIDE_SCRIPT = """
<script type="text/javascript">
async function main() {
  let pyodide = await loadPyodide();
  for (const archive of [{{ data_archives }}]) {
    let zipResponse = await fetch(archive);
    let zipBinary = await zipResponse.arrayBuffer();
    await pyodide.unpackArchive(zipBinary, "zip");
  }
  await pyodide.loadPackage("micropip");
  await pyodide.runPythonAsync(`
    import micropip
    await micropip.install([{{ env_spec }}]);
  `);
  code = `{{ code }}`
  await pyodide.runPythonAsync(code);
}
const run_main_on_load = () => {
  if (typeof loadPyodide !== 'undefined') {
    main();
  } else {
    setTimeout(run_main_on_load, 100);
  }
};
run_main_on_load();
</script>
"""

INIT_SERVICE_WORKER = """
<script type="text/javascript">
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('./serviceWorker.js').then(reg => {
    reg.onupdatefound = () => {
      const installingWorker = reg.installing;
      installingWorker.onstatechange = () => {
        if (installingWorker.state === 'installed' &&
            navigator.serviceWorker.controller) {
          // Reload page if service worker is replaced
          location.reload();
        }
      }
    }
  })
}
</script>
"""

@dataclasses.dataclass
class DummyRequirement:
    url: str
    name: str = 'DUMMY'
    specifier: str = ''


def make_index(files, title=None, manifest=True):
    if manifest:
        manifest = 'site.webmanifest'
        favicon = 'images/favicon.ico'
        apple_icon = 'images/apple-touch-icon.png'
    else:
        manifest = favicon = apple_icon = None
    items = {label: './'+os.path.basename(f) for label, f in sorted(files.items())}
    return INDEX_TEMPLATE.render(
        items=items, manifest=manifest, apple_icon=apple_icon,
        favicon=favicon, title=title, PANEL_CDN=CDN_DIST
    )

def build_pwa_manifest(files, title=None, **kwargs) -> str:
    if len(files) > 1:
        title = title or 'Panel Applications'
        path = 'index.html'
    else:
        title = title or 'Panel Applications'
        path = list(files.values())[0]
    return PWA_MANIFEST_TEMPLATE.render(
        name=title,
        path=path,
        **kwargs
    )


def collect_python_requirements(
    code: str | os.PathLike | t.IO,
    requirements: list[str] | t.Literal['auto'] | os.PathLike = 'auto',
    panel_version: t.Literal['auto', 'local'] | str = 'auto',
    http_patch: bool = True,
) -> list[str]:
    """
    Make sense of python requirements for our Panel script.

    Arguments
    ---------
    app: str | os.PathLike | IO,
        The filename of the Panel/Bokeh application to convert.
    requirements: list[str] | os.PathLike | Literal['auto']
        The list of requirements to include (in addition to Panel).
    panel_version: Literal['auto', 'local'] | str
        The panel release version to use in the exported HTML.
    http_patch: bool
        Whether to patch the HTTP request stack with the pyodide-http library
        to allow urllib3 and requests to work.
    """
    # Environment
    if panel_version == 'local':
        panel_req = './' + str(PANEL_LOCAL_WHL.as_posix()).split('/')[-1]
        bokeh_req = './' + str(BOKEH_LOCAL_WHL.as_posix()).split('/')[-1]
    elif panel_version == 'auto':
        panel_req = PANEL_CDN_WHL
        bokeh_req = BOKEH_CDN_WHL
    else:
        panel_req = f'panel=={panel_version}'
        bokeh_req = f'bokeh=={BOKEH_VERSION}'
    collected_requirements = [bokeh_req, panel_req]
    if http_patch:
        collected_requirements.append('pyodide-http')

    requirements_root = os.getcwd()
    resolved_reqs: list[str]
    if requirements == 'auto':
        if hasattr(code, 'read'):
            source = code.read()
        else:
            path = pathlib.Path(code)
            application = build_single_handler_application(path.absolute())
            source = application._handlers[0]._runner.source
        resolved_reqs = find_requirements(source)
    elif isinstance(requirements, (str, os.PathLike)) and pathlib.Path(requirements).is_file():
        requirements_root = os.path.dirname(requirements)
        resolved_reqs = (
            pathlib.Path(requirements).read_text(encoding='utf-8').splitlines()
        )
    elif isinstance(requirements, list):
        resolved_reqs = requirements
    else:
        raise ValueError(
            f'Requirements {requirements!r} could not be resolved. '
            'Provide a list of requirement specs, a path to a requirements.txt '
            'file that exists on disk or \'auto\' as a literal.'
        )

    for raw_req in resolved_reqs:
        stripped_req = raw_req.split('#')[0].strip()
        if not len(stripped_req) > 0:
            continue
        try:
            req = Requirement(stripped_req)
        except ValueError as e:
            if stripped_req.endswith('.whl'):
                req = t.cast('Requirement', DummyRequirement(stripped_req))
            else:
                raise ValueError(f'Requirements parser raised following error: {e}') from e

        if req.name in ('panel', 'bokeh'):
            continue
        elif req.url is not None:
            parsed_req = urlparse(req.url)
            if parsed_req.scheme in ('https', 'http'):
                collected_requirements.append(req.url)
            elif parsed_req.scheme in ('file', ''):
                check_path = parsed_req.path
                check_path = os.path.normpath(
                    os.path.join(requirements_root, check_path)
                )
                if os.path.exists(check_path):
                    collected_requirements.append(
                        f'file:{check_path}'
                    )  # make a custom URL so things can be handled as a URL
                else:
                    raise ValueError(f'Could not verify path for {req}. Make sure the file is available if it is a local wheel.')
        else:
            collected_requirements.append(f'{req.name}{req.specifier}')

    return collected_requirements


def pack_files(filemap: dict, destination: str | os.PathLike | t.IO):
    """
    Pack files into a zipfile for distribution

    Arguments
    ---------
    filemap: dict
        A dictionary mapping a local file to an archive name
    destination: str | os.PathLike | IO
        where to put the output zip
    """
    with ZipFile(destination, 'w') as packfile:
        for fname, arcname in filemap.items():
            packfile.write(fname, arcname=arcname)


def loading_resources(template, inline) -> list[str]:
    css_resources = []
    if template in (BASE_TEMPLATE, FILE):
        # Add loading.css if not served from Panel template
        if inline:
            svg_name = f'{config.loading_spinner}_spinner.svg'
            svg_b64 = base64.b64encode((DIST_DIR / 'assets' / svg_name).read_bytes()).decode('utf-8')
            loading_base = (
                DIST_DIR / "css" / "loading.css"
            ).read_text(encoding='utf-8').replace(
                f'../assets/{svg_name}', f'data:image/svg+xml;base64,{svg_b64}'
            )
            loading_style = f'<style type="text/css">\n{loading_base}\n</style>'
        else:
            loading_style = f'<link rel="stylesheet" href="{CDN_DIST}css/loading.css" type="text/css" />'
        css_resources.append(loading_style)
    spinner_css = loading_css(
        config.loading_spinner, config.loading_color, config.loading_max_height
    )
    css_resources.append(
        f'<style type="text/css">\n{spinner_css}\n</style>'
    )
    return css_resources

def script_to_html(
    filename: str | os.PathLike | t.IO,
    requirements: list[str] = [],
    app_resources: str | os.PathLike | None = None,
    js_resources: t.Literal['auto'] | list[str] = 'auto',
    css_resources: t.Literal['auto'] | list[str] | None = 'auto',
    runtime: Runtimes = 'pyodide',
    prerender: bool = True,
    panel_version: t.Literal['auto', 'local'] | str = 'auto',
    local_prefix: str = LOCAL_PREFIX,
    manifest: str | None = None,
    inline: bool = False,
    compiled: bool = True
) -> tuple[str, str | None, list[str]]:
    """
    Converts a Panel or Bokeh script to a standalone WASM Python
    application.

    Parameters
    ----------
    filename: str | Path | IO
        The filename of the Panel/Bokeh application to convert.
    requirements: list[str]
        The preprocessed, micropip-compatible list of requirements to include.
    app_resources: os.PathLike
        relative path of zip with data to extract
    js_resources: 'auto' | list[str]
        The list of JS resources to include in the exported HTML.
    css_resources: 'auto' | list[str] | None
        The list of CSS resources to include in the exported HTML.
    runtime: 'pyodide' | 'pyscript'
        The runtime to use for running Python in the browser.
    prerender: bool
        Whether to pre-render the components so the page loads.
    panel_version: Literal['auto', 'local'] | str
        The panel release version to use in the exported HTML.
    local_prefix: str
        Prefix for the path to serve local wheel files from.
    inline: bool
        Whether to inline resources.
    compiled: bool
        Whether to use pre-compiled pyodide bundles.
    """
    # Run script
    if hasattr(filename, 'read'):
        handler = CodeHandler(source=filename.read(), filename='convert.py')
        app_name = f'app-{str(uuid.uuid4())}'
        app = Application(handler)
    else:
        path = pathlib.Path(filename)
        app_name = '.'.join(path.name.split('.')[:-1])
        app = build_single_handler_application(str(path.absolute()))
    document = Document()
    document._session_context = lambda: MockSessionContext(document=document)  # type: ignore
    with set_curdoc(document):
        app.initialize_document(document)
        state._on_load(None)
    source = app._handlers[0]._runner.source

    if not document.roots:
        raise RuntimeError(
            f'The file {filename} does not publish any Panel contents. '
            'Ensure you have marked items as servable or added models to '
            'the bokeh document manually.'
        )

    # Execution
    post_code = POST_PYSCRIPT if runtime == 'pyscript' else POST
    # Escape javascript-style format strings.
    source = source.replace('${', '&#36;{')
    code = '\n'.join([PRE, source, post_code])
    web_worker = None
    if css_resources is None:
        css_resources = []
    if runtime.startswith('pyscript'):
        if js_resources == 'auto':
            js_resources = [PYSCRIPT_JS]
        if css_resources == 'auto':
            css_resources = [PYSCRIPT_CSS, PYSCRIPT_CSS_OVERRIDES]
        elif not css_resources:
            css_resources = []
        pyconfig = json.dumps({
            'packages': requirements,
            'plugins': ['!error'],
            'files': {app_resources: './*'} if app_resources else {},
        })
        css_resources.append('<style type="text/css">.py-error { display: none; }</style>')
        if 'worker' in runtime:
            plot_script = f'<script type="py" async worker config=\'{pyconfig}\' src="{app_name}.py"></script>'
            web_worker = code
        else:
            plot_script = f'<script type=\'py\' config=\'{pyconfig}\'>{code}</script>'
    else:
        if css_resources == 'auto':
            css_resources = []
        data_archives = f'{repr(app_resources)}' if app_resources else ''
        env_spec = ', '.join([repr(req) for req in requirements])
        code = code.encode('unicode_escape').decode('utf-8').replace('`', r'\`')
        if runtime == 'pyodide-worker':
            if js_resources == 'auto':
                js_resources = []
            worker_handler = WORKER_HANDLER_TEMPLATE.render({
                'name': app_name,
                'loading_spinner': config.loading_spinner
            })
            web_worker = WEB_WORKER_TEMPLATE.render({
                'PYODIDE_URL': PYODIDE_PYC_URL if compiled else PYODIDE_URL,
                'data_archives': data_archives,
                'env_spec': env_spec,
                'code': code
            })
            plot_script = wrap_in_script_tag(worker_handler)
        else:
            if js_resources == 'auto':
                js_resources = [PYODIDE_PYC_JS if compiled else PYODIDE_JS]
            script_template = _pn_env.from_string(PYODIDE_SCRIPT)
            plot_script = script_template.render({
                'data_archives': data_archives,
                'env_spec': env_spec,
                'code': code
            })

    if prerender:
        json_id = make_id()
        docs_json, render_items = standalone_docs_json_and_render_items(document)
        render_item = render_items[0]
        escaped_json = escape(serialize_json(docs_json), quote=False)
        plot_script += wrap_in_script_tag(escaped_json, "application/json", json_id)
        plot_script += wrap_in_script_tag(script_for_render_items(json_id, render_items))
    else:
        render_item = RenderItem(
            token='',
            roots=document.roots,
            use_for_title=False
        )
        render_items = [render_item]

    # Prepare template
    template = document.template
    if template is None:
        template = BASE_TEMPLATE
    elif isinstance(template, str):
        template = get_env().from_string("{% extends base %}\n" + template)

    # Collect resources
    resources = Resources(mode='inline' if inline else 'cdn')
    css_resource_tags = loading_resources(template, inline)
    css_resources = list(css_resources) + css_resource_tags if isinstance(css_resources, list) else css_resource_tags
    with set_curdoc(document):
        bundle = bundle_resources(document.roots, resources)
    bokeh_js_str = bundle._render_js()
    bokeh_css_str = bundle._render_css()
    extra_js_tags = [INIT_SERVICE_WORKER, bokeh_js_str] if manifest else [bokeh_js_str]
    bokeh_js = '\n'.join(js_resources + extra_js_tags)
    bokeh_css = '\n'.join([bokeh_css_str] + css_resources)

    collected_urls = _collect_bundle_urls(bundle, js_resources + extra_js_tags, css_resources)
    collected_urls.extend(_collect_document_stylesheet_urls(document))

    if config.design:
        try:
            design_res = config.design().resolve_resources(cdn=True, include_theme=True)
            for rtype in ('js', 'css', 'js_modules'):
                for url in design_res.get(rtype, {}).values():
                    if url and url not in collected_urls and not url.startswith('data:'):
                        collected_urls.append(url)
        except Exception:
            pass

    # Configure template
    template_variables = document._template_variables
    context = template_variables.copy()
    context.update(dict(
        title=document.title,
        bokeh_js=bokeh_js,
        bokeh_css=bokeh_css,
        plot_script=plot_script,
        docs=render_items,
        base=BASE_TEMPLATE,
        macros=MACROS,
        doc=render_item,
        roots=render_item.roots,
        manifest=manifest,
        dist_url=CDN_DIST
    ))

    # Render
    html = template.render(context)
    html = (html
        .replace('<body>', f'<body class="{LOADING_INDICATOR_CSS_CLASS} pn-{config.loading_spinner}">')
    )
    if runtime == 'pyscript-worker':
        # pyscript-worker apps must have strict cross-origin policies
        html = (html
            .replace('<script type="text/javascript"', '<script type="text/javascript" crossorigin="anonymous"')
            .replace('<link rel="stylesheet"', '<link rel="stylesheet" crossorigin="anonymous"')
            .replace('<link rel="icon"', '<link rel="icon" crossorigin="anonymous"')
        )
    return html, web_worker, collected_urls


def convert_app(
    app: str | os.PathLike,
    dest_path: str | os.PathLike | None = None,
    requirements: list[str] | t.Literal['auto'] | os.PathLike = 'auto',
    resources: list[str] | list[os.PathLike] | None = None,
    runtime: Runtimes = 'pyodide-worker',
    prerender: bool = True,
    manifest: str | None = None,
    panel_version: t.Literal['auto', 'local'] | str = 'auto',
    local_prefix: str = LOCAL_PREFIX,
    http_patch: bool = True,
    inline: bool = False,
    compiled: bool = False,
    verbose: bool = True,
) -> tuple[str, str, list[str]] | None:
    if dest_path is None:
        dest_path = pathlib.Path('./')
    elif not isinstance(dest_path, pathlib.PurePath):
        dest_path = pathlib.Path(dest_path)

    app_folder = os.path.dirname(app)
    app_name = '.'.join(os.path.basename(app).split('.')[:-1])

    # Obtain source
    app_source = ''
    try:
        app_path = pathlib.Path(app)
        if app_path.is_file() and app_path.suffix == '.py':
            app_source = app_path.read_text(encoding='utf-8')
    except Exception:
        pass
    source_exts = _parse_extension_calls_from_source(app_source) if app_source else []

    parsed_requirements = collect_python_requirements(
        app, requirements, panel_version=panel_version, http_patch=http_patch
    )
    # prepare wheels to be available via emscripten MEMFS
    parsed_requirements_rewritten = []
    wheels2pack: dict[str | os.PathLike, str] = {}

    for req in parsed_requirements:
        try:
            req_as_url = urlparse(req)
            if req_as_url.scheme == 'file':
                wheel_name = os.path.basename(req_as_url.path)
                emfs_wheel_path = 'packed_wheels' + '/' + wheel_name
                parsed_requirements_rewritten.append(f'emfs:{emfs_wheel_path}')
                wheels2pack[req_as_url.path] = emfs_wheel_path
            else:
                parsed_requirements_rewritten.append(req)
        except ValueError:
            # no url, so must be a properly formatted requirement
            parsed_requirements_rewritten.append(req)

    # make a zip out of resources
    resources_validated: dict[str | os.PathLike, str] = {}
    for resourcepath in ([] if resources is None else resources):
        commonpath = pathlib.Path(
            os.path.commonpath(
                [os.path.abspath(resourcepath), os.path.abspath(app_folder)]
            )
        )
        if commonpath.resolve() == pathlib.Path(app_folder).resolve():
            resources_validated[resourcepath] = os.path.relpath(
                resourcepath, app_folder
            )
        else:
            raise ValueError('resources have to be in a folder rootable at the app-directory')

    # resources unpacked into emscripten MEMFS
    app_resources = {**wheels2pack, **resources_validated}
    if app_resources:
        app_resources_packfile = f'{app_name}.resources.zip'
        pack_files(app_resources, os.path.join(dest_path, app_resources_packfile))
    else:
        app_resources_packfile = None

    # try to convert the app to a standalone package
    try:
        with set_resource_mode('inline' if inline else 'cdn'):
            html, worker, collected_urls = script_to_html(
                app,
                requirements=parsed_requirements_rewritten,
                app_resources=app_resources_packfile,
                runtime=runtime,
                prerender=prerender,
                manifest=manifest,
                panel_version=panel_version,
                inline=inline,
                compiled=compiled,
                local_prefix=local_prefix
            )
    except KeyboardInterrupt:
        return
    except Exception as e:
        print(f'Failed to convert {app} to {runtime} target: {e}')
        return

    # ---------- Localize assets: detect extensions, collect URLs, download, rewrite ----------
    if verbose:
        print(f'  [assets] Detecting extensions for {app_name}...')

    doc_exts = _detect_extensions_from_document(state.curdoc) if state.curdoc else []
    all_ext_names: list[str] = []
    seen_ext: set[str] = set()
    for e in source_exts + doc_exts:
        if e not in seen_ext:
            seen_ext.add(e)
            all_ext_names.append(e)

    if verbose and all_ext_names:
        print(f'  [assets] Detected extensions: {", ".join(all_ext_names)}')

    all_resource_urls: list[str] = list(collected_urls)
    seen_urls: set[str] = set(all_resource_urls)

    def _merge_urls(group: dict[str, list[str]]):
        for rtype in ('js', 'css', 'js_modules'):
            for url in group.get(rtype, []):
                if url and url not in seen_urls and not url.startswith('data:'):
                    seen_urls.add(url)
                    all_resource_urls.append(url)

    _merge_urls(_resolve_extension_resource_urls(all_ext_names))
    _merge_urls(_resolve_design_theme_resource_urls())
    _merge_urls(_resolve_runtime_resource_urls(runtime, compiled=compiled))

    for req in parsed_requirements:
        try:
            req_url = urlparse(req)
            if req_url.scheme in ('https', 'http') and req not in seen_urls:
                seen_urls.add(req)
                all_resource_urls.append(req)
        except ValueError:
            pass

    if verbose:
        print(f'  [assets] Localizing {len(all_resource_urls)} resource URLs...')

    url_mapping, _assets_path, local_asset_paths = build_local_assets(
        all_resource_urls,
        dest_path,
        assets_subdir='assets',
        verbose=verbose,
    )

    # Generate and write the asset manifest
    manifest_data = {
        'version': 1,
        'extensions': all_ext_names,
        'mapping': url_mapping,
        'assets': local_asset_paths,
    }
    manifest_path = dest_path / f'{app_name}.assets.json'
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, sort_keys=True),
        encoding='utf-8',
    )

    # Rewrite all CDN / remote URLs in HTML and worker to local asset paths
    html = rewrite_local_urls(html, url_mapping)
    if worker:
        worker = rewrite_local_urls(worker, url_mapping)

    # Collect all resources that should be cached by the service worker
    # (now only local paths — no remote CDN URLs)
    all_resources: list[str] = list(local_asset_paths)
    seen = set(all_resources)

    def _add_res(path: str):
        if path and path not in seen:
            seen.add(path)
            all_resources.append(path)

    # write out the app
    filename = f'{app_name}.html'
    _add_res(f'./{filename}')
    _add_res(f'./{app_name}.assets.json')

    with open(dest_path / filename, 'w', encoding='utf-8') as out:
        out.write(html)
    if 'worker' in runtime and worker:
        ext = 'py' if runtime.startswith('pyscript') else 'js'
        worker_filename = f'{app_name}.{ext}'
        _add_res(f'./{worker_filename}')
        with open(dest_path / worker_filename, 'w', encoding="utf-8") as out:
            out.write(worker)

    # Add resources zip to cache list
    if app_resources_packfile:
        _add_res(f'./{app_resources_packfile}')

    # Add user-specified resource files (relative paths)
    for rel_path in resources_validated.values():
        _add_res(f'./{rel_path}')

    if verbose:
        print(f'Successfully converted {app} to {runtime} target and wrote output to {filename}.')
    return (app_name.replace('_', ' '), filename, all_resources)


def _convert_process_pool(
    apps: Sequence[str | os.PathLike],
    dest_path: os.PathLike | str | None = None,
    max_workers: int = 4,
    requirements: list[str] | t.Literal['auto'] | os.PathLike = 'auto',
    **kwargs
) -> tuple[dict[str, str], list[str]]:
    import multiprocessing as mp

    from concurrent.futures import ProcessPoolExecutor

    files: dict[str, str] = {}
    all_resources: list[str] = []
    seen: set[str] = set()
    groups = [apps[i:i+max_workers] for i in range(0, len(apps), max_workers)]
    for group in groups:
        with ProcessPoolExecutor(
            max_workers=max_workers, mp_context=mp.get_context('spawn')
        ) as executor:
            futures = []
            for app in group:
                if isinstance(requirements, dict):
                    app_requires = requirements.get(app, 'auto')
                else:
                    app_requires = requirements
                f = executor.submit(
                    convert_app, app, dest_path, requirements=app_requires, **kwargs
                )
                futures.append(f)
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    name, filename, resources = result
                    files[name] = filename
                    for r in resources:
                        if r not in seen:
                            seen.add(r)
                            all_resources.append(r)
    return files, all_resources


def convert_apps(
    apps: str | os.PathLike | Sequence[str | os.PathLike],
    dest_path: str | os.PathLike | None = None,
    title: str | None = None,
    runtime: Runtimes = 'pyodide-worker',
    requirements: list[str] | t.Literal['auto'] | os.PathLike = 'auto',
    resources: list[str] | list[os.PathLike] | None = None,
    prerender: bool = True,
    build_index: bool = True,
    build_pwa: bool = True,
    pwa_config: dict[t.Any, t.Any] = {},
    max_workers: int = 4,
    panel_version: t.Literal['auto', 'local'] | str = 'auto',
    local_prefix: str = LOCAL_PREFIX,
    http_patch: bool = True,
    inline: bool = False,
    compiled: bool = False,
    verbose: bool = True,
):
    """
    Parameters
    ----------
    apps: str | List[str]
        The filename(s) of the Panel/Bokeh application(s) to convert.
    dest_path: str | pathlib.Path
        The directory to write the converted application(s) to.
    title: str | None
        A title for the application(s). Also used to generate unique
        name for the application cache to ensure.
    runtime: 'pyodide' | 'pyscript' | 'pyodide-worker'
        The runtime to use for running Python in the browser.
    requirements: 'auto' | List[str] | os.PathLike | Dict[str, 'auto' | List[str] | os.PathLike]
        The list of requirements to include (in addition to Panel).
        By default automatically infers dependencies from imports
        in the application. May also provide path to a requirements.txt
    prerender: bool
        Whether to pre-render the components so the page loads.
    build_index: bool
        Whether to write an index page (if there are multiple apps).
    build_pwa: bool
        Whether to write files to define a progressive web app (PWA) including
        a manifest and a service worker that caches the application locally
    pwa_config: Dict[Any, Any]
        Configuration for the PWA including (see https://developer.mozilla.org/en-US/docs/Web/Manifest)

          - display: Display options ('fullscreen', 'standalone', 'minimal-ui' 'browser')
          - orientation: Preferred orientation
          - background_color: The background color of the splash screen
          - theme_color: The theme color of the application
    max_workers: int
        The maximum number of parallel workers
    panel_version: Literal['auto' | 'local'] | str
'       The panel version to include.
    local_prefix: str
        Prefix for the path to serve local wheel files from.
    http_patch: bool
        Whether to patch the HTTP request stack with the pyodide-http library
        to allow urllib3 and requests to work.
    inline: bool
        Whether to inline resources.
    compiled: bool
        Whether to use the compiled and faster version of Pyodide.
    """
    if isinstance(apps, (str, os.PathLike)):
        apps = [apps]
    if dest_path is None:
        dest_path = pathlib.Path('./')
    elif not isinstance(dest_path, pathlib.PurePath):
        dest_path = pathlib.Path(dest_path)
    dest_path.mkdir(parents=True, exist_ok=True)

    manifest = 'site.webmanifest' if build_pwa else None

    if isinstance(requirements, dict):
        app_requirements = {}
        for app in apps:
            matches = [
                deps for name, deps in requirements.items()
                if str(app).endswith(name.replace(os.path.sep, '/'))
            ]
            app_requirements[app] = matches[0] if matches else 'auto'
    else:
        app_requirements = requirements

    kwargs = {
        'requirements': app_requirements,
        'resources': resources if resources else [],
        'runtime': runtime,
        'prerender': prerender,
        'manifest': manifest,
        'panel_version': panel_version,
        'http_patch': http_patch,
        'inline': inline,
        'verbose': verbose,
        'compiled': compiled,
        'local_prefix': local_prefix,
    }

    if state._is_pyodide:
        files_labels: dict[str, str] = {}
        all_collected_resources: list[str] = []
        seen_resources: set[str] = set()
        for app in apps:
            result = convert_app(app, dest_path, **kwargs)  # type: ignore
            if result is not None:
                name, filename, resources = result
                files_labels[name] = filename
                for r in resources:
                    if r not in seen_resources:
                        seen_resources.add(r)
                        all_collected_resources.append(r)
    else:
        files_labels, all_collected_resources = _convert_process_pool(
            apps, dest_path, max_workers=max_workers, **kwargs  # type: ignore
        )
        seen_resources = set(all_collected_resources)

    def _add_global_res(path: str):
        if path and path not in seen_resources:
            seen_resources.add(path)
            all_collected_resources.append(path)

    if build_index and len(files_labels) >= 1:
        index = make_index(files_labels, manifest=build_pwa, title=title)
        with open(dest_path / 'index.html', 'w') as f:
            f.write(index)
        _add_global_res('./index.html')
        if verbose:
            print('Successfully wrote index.html.')

    if not build_pwa:
        return

    # Write icons
    imgs_path = (dest_path / 'images')
    imgs_path.mkdir(exist_ok=True)
    img_rel = []
    for img in PWA_IMAGES:
        with open(imgs_path / img.name, 'wb') as f:
            f.write(img.read_bytes())
        img_path = f'images/{img.name}'
        img_rel.append(img_path)
        _add_global_res(f'./{img_path}')
    if verbose:
        print('Successfully wrote icons and images.')

    # Write manifest
    manifest_content = build_pwa_manifest(files_labels, title=title, **pwa_config)
    with open(dest_path / 'site.webmanifest', 'w', encoding='utf-8') as f:
        f.write(manifest_content)
    _add_global_res('./site.webmanifest')
    if verbose:
        print('Successfully wrote site.manifest.')

    # Write service worker
    worker = SERVICE_WORKER_TEMPLATE.render(
        uuid=uuid.uuid4().hex,
        name=title or 'Panel Pyodide App',
        pre_cache=', '.join([repr(p) for p in all_collected_resources])
    )
    with open(dest_path / 'serviceWorker.js', 'w', encoding='utf-8') as f:
        f.write(worker)
    if verbose:
        print('Successfully wrote serviceWorker.js.')
