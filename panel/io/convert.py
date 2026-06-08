from __future__ import annotations

import ast
import base64
import concurrent.futures
import dataclasses
import json
import os
import pathlib
import re
import typing as t
import uuid

from collections import Counter
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

ManifestCategory = t.Literal[
    'wheel', 'js', 'css', 'image', 'font', 'user-data',
    'runtime-url', 'html', 'service-worker',
]

ManifestOrigin = t.Literal[
    'bokeh-core',
    'panel-core',
    'panel-extension',
    'pane-widget',
    'theme',
    'user-cli-resource',
    'wheel-panel-dep',
    'wheel-auto-detected',
    'wheel-cli-list',
    'wheel-requirements-file',
    'wheel-local-file',
    'pwa-icon',
    'runtime-code',
    'app-html',
    'app-worker',
    'unknown',
]

CachePolicy = t.Literal[
    'precache', 'cache-first', 'network-first',
    'stale-while-revalidate', 'no-cache', 'runtime-only',
]

_URL_RE = re.compile(r'https?://[^\s"\'<>)]+')


@dataclasses.dataclass
class ManifestEntry:
    """
    Single resource entry in the unified asset manifest.

    Every resource tracked by the convert pipeline is represented as one
    ManifestEntry.  Collectors, URL rewrites, the service‑worker generator,
    diagnostics and the final report all operate on this same structure.
    """
    page: str
    category: ManifestCategory
    origin: ManifestOrigin
    owner: str
    local_path: str | None = None
    remote_url: str | None = None
    cache_policy: CachePolicy = 'runtime-only'
    localized: bool = False
    size_kb: float | None = None
    integrity: str | None = None
    note: str = ''
    failure_reason: str = ''

    @property
    def display_name(self) -> str:
        return self.local_path or self.remote_url or self.owner

    def mark_localized(self, local_path: str, size_kb: float | None = None) -> None:
        """Called after the resource is successfully written/copied to disk."""
        self.local_path = local_path
        self.localized = True
        if size_kb is not None:
            self.size_kb = size_kb
        self.failure_reason = ''

    def mark_failed(self, reason: str) -> None:
        """Called when a resource could not be localized or downloaded."""
        self.localized = False
        self.failure_reason = reason

    def set_cache_policy(self, policy: CachePolicy) -> None:
        self.cache_policy = policy

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class DiagnosticIssue:
    severity: t.Literal['error', 'warning', 'info']
    category: t.Literal['missing', 'duplicate', 'non-localized', 'size-warning', 'cache-concern', 'inconsistency']
    resource: str
    message: str
    page: str = ''
    origin: str = ''
    owner: str = ''


@dataclasses.dataclass
class AssetManifest:
    """
    Unified asset manifest – single source of truth for the convert pipeline.
    """
    generated_at: str
    runtime: str
    is_pwa: bool
    dest_path: str
    entries: list[ManifestEntry] = dataclasses.field(default_factory=list)
    total_size_kb: float = 0.0

    def add(self, entry: ManifestEntry) -> ManifestEntry:
        self.entries.append(entry)
        if entry.size_kb:
            self.total_size_kb += entry.size_kb
        return entry

    def filter(self, **kwargs) -> list[ManifestEntry]:
        result = list(self.entries)
        for k, v in kwargs.items():
            result = [e for e in result if getattr(e, k) == v]
        return result

    def pages(self) -> list[str]:
        return sorted({e.page for e in self.entries if e.page != '__global__'})

    def to_dict(self) -> dict:
        return {
            'generated_at': self.generated_at,
            'runtime': self.runtime,
            'is_pwa': self.is_pwa,
            'dest_path': self.dest_path,
            'total_size_kb': round(self.total_size_kb, 2),
            'entries': [e.to_dict() for e in self.entries],
        }

    def to_markdown(self, issues: list['DiagnosticIssue'] | None = None) -> str:
        from datetime import datetime
        lines = [
            '# Panel Convert Asset Manifest',
            '',
            f'- **Generated at**: {self.generated_at}',
            f'- **Runtime**: {self.runtime}',
            f'- **PWA enabled**: {self.is_pwa}',
            f'- **Output dir**: `{self.dest_path}`',
            f'- **Total tracked assets**: {len(self.entries)}',
            f'- **Total estimated size**: {self.total_size_kb:.1f} KB',
            '',
        ]
        per_page: dict[str, list[ManifestEntry]] = {}
        for e in self.entries:
            per_page.setdefault(e.page, []).append(e)

        for page in sorted(per_page.keys()):
            page_entries = per_page[page]
            page_label = 'Global / Service Worker' if page == '__global__' else page
            lines.extend([
                f'## Page: {page_label}',
                '',
                f'- **Asset count**: {len(page_entries)}',
                f'- **Estimated size**: {sum(e.size_kb or 0 for e in page_entries):.1f} KB',
                '',
                '| Category | Origin | Owner | Local Path | Remote URL | Cache Policy | Localized | Size (KB) | Failure |',
                '|----------|--------|-------|------------|------------|--------------|-----------|-----------|---------|',
            ])
            for e in sorted(page_entries, key=lambda x: (x.category, x.origin, x.owner)):
                lp = f'`{e.local_path}`' if e.local_path else '-'
                ru = f'`{e.remote_url}`' if e.remote_url else '-'
                size = f'{e.size_kb:.1f}' if e.size_kb else '-'
                fail = e.failure_reason if e.failure_reason else '-'
                lines.append(
                    f'| {e.category} | {e.origin} | {e.owner} | {lp} | {ru} | {e.cache_policy} | {e.localized} | {size} | {fail} |'
                )
            lines.append('')

        by_origin: dict[str, list[ManifestEntry]] = {}
        for e in self.entries:
            by_origin.setdefault(e.origin, []).append(e)
        lines.extend([
            '## Summary by Origin',
            '',
            '| Origin | Count | Size (KB) |',
            '|--------|-------|-----------|',
        ])
        for origin in sorted(by_origin.keys()):
            es = by_origin[origin]
            total = sum(e.size_kb or 0 for e in es)
            lines.append(f'| {origin} | {len(es)} | {total:.1f} |')
        lines.append('')

        if issues:
            lines.extend(['## Diagnostics', ''])
            by_sev: dict[str, list[DiagnosticIssue]] = {}
            for issue in issues:
                by_sev.setdefault(issue.severity, []).append(issue)
            for sev in ('error', 'warning', 'info'):
                if sev not in by_sev:
                    continue
                lines.append(f'### {sev.upper()} ({len(by_sev[sev])})')
                lines.append('')
                for issue in by_sev[sev]:
                    pg = f' (page: `{issue.page}`)' if issue.page else ''
                    own = f' owner=`{issue.owner}`' if issue.owner else ''
                    lines.append(f'- **[{issue.category}]** {issue.resource}{pg}{own}: {issue.message}')
                lines.append('')

        return '\n'.join(lines)


@dataclasses.dataclass
class ThemeInfo:
    name: str
    css_files: list[str]
    bokeh_theme: str | None = None


def _url_to_category(path: str) -> ManifestCategory:
    ext = pathlib.Path(urlparse(path).path).suffix.lower()
    if ext in ('.js', '.mjs'):
        return 'js'
    if ext == '.css':
        return 'css'
    if ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp'):
        return 'image'
    if ext in ('.woff', '.woff2', '.ttf', '.otf', '.eot'):
        return 'font'
    if ext == '.whl':
        return 'wheel'
    return 'user-data'


def _is_remote_url(path: str) -> bool:
    parsed = urlparse(path)
    return parsed.scheme in ('http', 'https')


_STATIC_EXTS = {
    '.js', '.mjs', '.css', '.wasm', '.data', '.whl', '.zip',
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp',
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
    '.json', '.map', '.csv', '.parquet', '.arrow',
}

_KNOWN_STATIC_HOSTS = (
    'cdn.holoviz.org',
    'cdn.bokeh.org',
    'pyscript.net',
    'cdn.jsdelivr.net',
    'unpkg.com',
    'cdnjs.cloudflare.com',
    'cdn.jsdelivr.net',
)


def _is_static_resource_url(url: str) -> bool:
    """
    Decide whether a URL found in source code references a static asset
    (wheel, JS, CSS, image, zip archive) vs. a business API endpoint.

    Used when scanning workers so that we do not flag legitimate REST /
    RPC calls as missing assets.
    """
    stripped = url.strip().strip("'\"`")
    if not stripped:
        return False
    if stripped.startswith(('http://', 'https://', '//', './', '/', '../')):
        parsed = urlparse(stripped)
        ext = pathlib.Path(parsed.path).suffix.lower()
        if ext in _STATIC_EXTS:
            return True
        if parsed.netloc and parsed.netloc.endswith(_KNOWN_STATIC_HOSTS):
            return True
        return False
    # Relative path or bare filename with extension
    if '/' in stripped or stripped.startswith('.'):
        ext = pathlib.Path(urlparse(stripped).path).suffix.lower()
        return ext in _STATIC_EXTS
    # Bare name – could be a wheel name in micropip.install([...])
    # Only treat as static if it has a static extension or looks like a wheel
    if stripped.endswith('.whl'):
        return True
    return False


_IMPORT_SCRIPTS_RE = re.compile(r'importScripts\s*\(\s*([^)]+)\s*\)')
_MICROPIP_INSTALL_RE = re.compile(
    r'micropip\.install\s*\(\s*(\[[^\]]*\]|[^)]+)\s*\)'
)
_FETCH_RE = re.compile(r'fetch\s*\(\s*([^,)]+)')
_QUOTED_STR_RE = re.compile(r'''["']([^"']+)["']''')


def _scan_worker_for_static_urls(
    text: str,
    worker_filename: str,
) -> list[tuple[str, str]]:
    """
    Scan a worker JS/PY file for URLs that reference static assets.
    Returns list of (url, context) where context describes where the URL
    was found (importScripts / micropip.install / fetch / string-literal).
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(url: str, ctx: str) -> None:
        u = url.strip().strip("'\"`")
        if not u or u in seen or not _is_static_resource_url(u):
            return
        seen.add(u)
        results.append((u, ctx))

    for m in _IMPORT_SCRIPTS_RE.finditer(text):
        for inner in _QUOTED_STR_RE.findall(m.group(1)):
            _add(inner, f'{worker_filename}:importScripts')
        for raw in m.group(1).split(','):
            _add(raw, f'{worker_filename}:importScripts')

    for m in _MICROPIP_INSTALL_RE.finditer(text):
        arg = m.group(1).strip()
        for inner in _QUOTED_STR_RE.findall(arg):
            _add(inner, f'{worker_filename}:micropip.install')
        # Also try to split comma-separated list items
        for item in re.split(r'[,\s]+', arg.strip('[] \t\n')):
            if item:
                _add(item, f'{worker_filename}:micropip.install')

    for m in _FETCH_RE.finditer(text):
        arg = m.group(1).strip()
        for inner in _QUOTED_STR_RE.findall(arg):
            _add(inner, f'{worker_filename}:fetch')
        _add(arg, f'{worker_filename}:fetch')

    # Catch-all: http(s) URLs in string literals (already static by nature)
    for m in _URL_RE.finditer(text):
        _add(m.group(0), f'{worker_filename}:string-literal')

    # Catch-all: all quoted strings that look like static resource paths
    for m in _QUOTED_STR_RE.finditer(text):
        s = m.group(1)
        if s.startswith(('http://', 'https://', '//', './', '/', '../')):
            _add(s, f'{worker_filename}:string-literal')
        elif '/' in s or s.startswith('.'):
            _add(s, f'{worker_filename}:string-literal')
        elif s.endswith('.whl'):
            _add(s, f'{worker_filename}:string-literal')

    return results


def _classify_resource_type(path: str) -> t.Literal['js', 'css', 'image', 'font', 'other']:
    ext = pathlib.Path(urlparse(path).path).suffix.lower()
    if ext in ('.js', '.mjs'):
        return 'js'
    if ext == '.css':
        return 'css'
    if ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp'):
        return 'image'
    if ext in ('.woff', '.woff2', '.ttf', '.otf', '.eot'):
        return 'font'
    return 'other'


def _classify_resource_source(
    path: str,
) -> t.Literal['cdn', 'local', 'inline', 'external']:
    if path.startswith('data:'):
        return 'inline'
    parsed = urlparse(path)
    if parsed.scheme in ('http', 'https'):
        if CDN_DIST in path or CDN_ROOT in path:
            return 'cdn'
        return 'external'
    return 'local'


def _get_file_size_kb(path: str, base_dir: pathlib.Path | None = None) -> float | None:
    parsed = urlparse(path)
    if parsed.scheme in ('http', 'https'):
        return None
    local_path = pathlib.Path(parsed.path)
    if not local_path.is_absolute() and base_dir:
        local_path = base_dir / local_path
    try:
        if local_path.is_file():
            return local_path.stat().st_size / 1024
    except OSError:
        pass
    return None


def _extract_runtime_urls(
    code: str,
    page: str,
    source_name: str = 'app code',
) -> list[ManifestEntry]:
    seen: set[str] = set()
    entries: list[ManifestEntry] = []

    def _add(url: str, detail: str) -> None:
        if url in seen:
            return
        seen.add(url)
        entries.append(ManifestEntry(
            page=page,
            category='runtime-url',
            origin='runtime-code',
            owner=f'{os.path.basename(source_name)}:{detail}',
            local_path=None,
            remote_url=url,
            cache_policy='runtime-only',
            localized=False,
            size_kb=None,
            note=detail,
        ))

    for match in _URL_RE.findall(code):
        if not match.endswith(('.whl', '.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico')):
            url = match.rstrip('.,;:)')
            _add(url, 'regex scan')
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in _URL_RE.findall(node.value):
                    if not match.endswith(('.whl', '.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico')):
                        url = match.rstrip('.,;:)')
                        _add(url, f'string literal L{getattr(node, "lineno", "?")}')
    except SyntaxError:
        pass
    return entries


def _detect_theme(document: Document) -> ThemeInfo | None:
    from ..theme.base import Design, Theme
    try:
        for root in document.roots:
            design = getattr(root, 'design', None)
            if design and isinstance(design, Design):
                theme = design.theme
                css_files = []
                if theme and theme.base_css:
                    css_files.append(str(theme.base_css))
                if theme and theme.css:
                    css_files.append(str(theme.css))
                bokeh_theme = None
                if theme and theme.bokeh_theme:
                    bokeh_theme = str(theme.bokeh_theme) if isinstance(theme.bokeh_theme, str) else type(theme.bokeh_theme).__name__
                return ThemeInfo(
                    name=type(design).__name__ + ':' + (theme._name if theme else 'default'),
                    css_files=css_files,
                    bokeh_theme=bokeh_theme,
                )
    except Exception:
        pass
    return None


def _collect_theme_css_resources(
    document: Document,
    manifest: AssetManifest,
    page: str,
    dest_path: pathlib.Path | None = None,
) -> None:
    theme_info = _detect_theme(document)
    if not theme_info:
        return
    for css_path in theme_info.css_files:
        css_path_str = str(css_path)
        remote = css_path_str if _is_remote_url(css_path_str) else None
        local = css_path_str if not _is_remote_url(css_path_str) else None
        size_kb = _get_file_size_kb(css_path_str, dest_path)
        manifest.add(ManifestEntry(
            page=page,
            category='css',
            origin='theme',
            owner=theme_info.name,
            local_path=local,
            remote_url=remote,
            cache_policy='cache-first',
            localized=bool(local and pathlib.Path(local).is_file()) or (bool(dest_path) and local and (dest_path / local).is_file()),
            size_kb=size_kb,
            note=f'Theme CSS from {theme_info.name}',
        ))


def _collect_resources_into_manifest(
    document: Document,
    manifest: AssetManifest,
    page: str,
    roots: list | None = None,
    dest_path: pathlib.Path | None = None,
) -> None:
    """
    Walks the document's model tree and adds every JS/CSS resource directly
    to the unified AssetManifest with full provenance (page, origin, owner).
    """
    from ..io.resources import CDN_DIST, CDN_ROOT, bundle_resources, Resources, ResourceComponent

    if roots is None:
        roots = list(document.roots)

    seen_urls: set[str] = set()

    component_resources: dict[str, tuple[ManifestOrigin, str]] = {}

    for model in document.models:
        model_id = model.id
        mcls = type(model)
        cls_name = mcls.__name__
        owner_prefix = f'{cls_name}#{model_id}'
        for attr in ('__javascript__', '__css__', '__js_modules__'):
            urls = getattr(mcls, attr, None)
            if not urls:
                continue
            if isinstance(urls, str):
                urls = [urls]
            for i, url in enumerate(urls):
                resource_type = 'javascript' if attr == '__javascript__' else (
                    'js_module' if attr == '__js_modules__' else 'css'
                )
                component_resources[url] = (
                    'pane-widget',
                    f'{owner_prefix}:{resource_type}[{i}]',
                )

        if isinstance(model, ResourceComponent) or hasattr(model, 'resolve_resources'):
            try:
                if hasattr(model, 'resolve_resources'):
                    resolved = model.resolve_resources(cdn=True)
                    for rname, rurl in resolved.get('js', {}).items():
                        component_resources[rurl] = ('pane-widget', f'{owner_prefix}:{rname}')
                    for rname, rurl in resolved.get('js_modules', {}).items():
                        component_resources[rurl] = ('pane-widget', f'{owner_prefix}:{rname}')
                    for rname, rurl in resolved.get('css', {}).items():
                        component_resources[rurl] = ('pane-widget', f'{owner_prefix}:{rname}')
            except Exception:
                pass

    try:
        res = Resources(mode='cdn')
        bundle = bundle_resources(roots, res)
    except Exception:
        bundle = None

    def _classify_origin(url: str) -> ManifestOrigin:
        if url in component_resources:
            return component_resources[url][0]
        if CDN_DIST in url and ('bundled/' in url or '@holoviz/panel' in url):
            return 'panel-extension'
        if 'panel.min.js' in url or (CDN_DIST in url and ('/js/' in url or '/css/' in url)):
            return 'panel-core'
        if 'bokeh' in url or CDN_ROOT in url:
            return 'bokeh-core'
        if CDN_DIST in url:
            return 'panel-extension'
        return component_resources.get(url, ('unknown', ''))[0]

    def _owner_for(url: str) -> str:
        if url in component_resources:
            return component_resources[url][1]
        origin = _classify_origin(url)
        if origin == 'panel-core':
            return 'panel-core-runtime'
        if origin == 'bokeh-core':
            return 'bokeh-core-runtime'
        if origin == 'panel-extension':
            return 'panel-extension-bundle'
        return urlparse(url).netloc

    def _add_entry(url: str, category: ManifestCategory) -> None:
        if url in seen_urls:
            return
        seen_urls.add(url)
        origin = _classify_origin(url)
        owner = _owner_for(url)
        is_remote = _is_remote_url(url)
        remote = url if is_remote else None
        local = None if is_remote else url
        size_kb = _get_file_size_kb(url, dest_path)
        manifest.add(ManifestEntry(
            page=page,
            category=category,
            origin=origin,
            owner=owner,
            local_path=local,
            remote_url=remote,
            cache_policy='cache-first',
            localized=not is_remote and (
                pathlib.Path(url).is_file() or (bool(dest_path) and (dest_path / url).is_file())
            ),
            size_kb=size_kb,
        ))

    if bundle:
        for url in bundle.js_files:
            _add_entry(str(url), 'js')
        for url in bundle.css_files:
            _add_entry(str(url), 'css')

    for rurl in component_resources:
        cat = _url_to_category(rurl)
        if cat in ('js', 'css'):
            _add_entry(rurl, cat)

    _collect_theme_css_resources(document, manifest, page, dest_path)


def diagnose_assets(
    manifest: AssetManifest,
) -> list[DiagnosticIssue]:
    issues: list[DiagnosticIssue] = []
    dest_path = pathlib.Path(manifest.dest_path)

    path_counter: dict[tuple[str, str], int] = Counter()
    for e in manifest.entries:
        key = (e.local_path or e.remote_url or e.owner, e.page)
        path_counter[key] += 1

    for entry in manifest.entries:
        display = entry.display_name
        if entry.failure_reason and entry.category != 'runtime-url':
            issues.append(DiagnosticIssue(
                severity='error',
                category='missing',
                resource=display,
                message=f'{entry.failure_reason}',
                page=entry.page,
                origin=entry.origin,
                owner=entry.owner,
            ))
            continue
        if entry.local_path and not entry.localized and entry.origin != 'runtime-code':
            lp = pathlib.Path(entry.local_path)
            if not lp.is_absolute():
                lp = dest_path / entry.local_path
            if not lp.is_file():
                issues.append(DiagnosticIssue(
                    severity='error',
                    category='missing',
                    resource=display,
                    message=f'Local {entry.category} file not found on disk at {entry.local_path}',
                    page=entry.page,
                    origin=entry.origin,
                    owner=entry.owner,
                ))
                continue
            if not entry.localized:
                entry.localized = True
        if entry.remote_url and not entry.localized and entry.category != 'runtime-url':
            issues.append(DiagnosticIssue(
                severity='warning',
                category='non-localized',
                resource=display,
                message=(
                    f'{entry.category.upper()} loaded from remote URL {entry.remote_url}, '
                    f'may fail offline'
                ),
                page=entry.page,
                origin=entry.origin,
                owner=entry.owner,
            ))
        if entry.category == 'runtime-url':
            issues.append(DiagnosticIssue(
                severity='warning',
                category='non-localized',
                resource=display,
                message='Runtime network URL referenced in code; must be reachable or cached for offline use',
                page=entry.page,
                origin=entry.origin,
                owner=entry.owner,
            ))

    # Duplicates across pages (same resource path used in multiple pages)
    global_counter: dict[str, list[str]] = {}
    for e in manifest.entries:
        key = e.local_path or e.remote_url
        if key:
            global_counter.setdefault(key, []).append(e.page)
    for path, pages in global_counter.items():
        if len(set(pages)) > 1:
            issues.append(DiagnosticIssue(
                severity='info',
                category='duplicate',
                resource=path,
                message=f'Resource included {len(pages)} times across pages: {", ".join(sorted(set(pages)))}',
                page=', '.join(sorted(set(pages))),
                origin='manifest-summary',
                owner='cross-page-duplicate',
            ))

    # Size warnings
    if manifest.total_size_kb > 10000:
        issues.append(DiagnosticIssue(
            severity='warning',
            category='size-warning',
            resource='<total>',
            message=f'Total assets exceed 10MB ({manifest.total_size_kb:.1f} KB), consider optimizing wheels and resources',
            page='__global__',
            origin='manifest-summary',
            owner='total-size',
        ))

    return issues


def print_diagnostics(issues: list[DiagnosticIssue]) -> None:
    if not issues:
        print('\n✅ No asset issues detected.')
        return
    by_severity: dict[str, list[DiagnosticIssue]] = {}
    for issue in issues:
        by_severity.setdefault(issue.severity, []).append(issue)
    print()
    for severity in ('error', 'warning', 'info'):
        if severity not in by_severity:
            continue
        sev_items = by_severity[severity]
        prefix = {'error': '❌', 'warning': '⚠️', 'info': 'ℹ️'}[severity]
        label = severity.upper()
        print(f'{prefix} {label}: {len(sev_items)} issue(s)')
        for issue in sev_items:
            origin = f' [{issue.origin}]' if issue.origin else ''
            owner = f' ({issue.owner})' if issue.owner else ''
            page = f' @ {issue.page}' if issue.page and issue.page != '__global__' else ''
            print(f'   [{issue.category}] {issue.resource}{origin}{owner}: {issue.message}{page}')
    print()


def write_assets_report(
    manifest: AssetManifest,
    issues: list[DiagnosticIssue],
    dest_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    json_path = dest_path / 'assets_report.json'
    md_path = dest_path / 'assets_report.md'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            **manifest.to_dict(),
            'issues': [dataclasses.asdict(i) for i in issues],
        }, f, indent=2, default=str)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(manifest.to_markdown(issues))
    return json_path, md_path


def _validate_manifest_vs_output(
    manifest: AssetManifest,
) -> list[DiagnosticIssue]:
    """
    Cross-check the manifest against what actually got written to disk and
    what URLs the generated HTML / service worker reference.  Updates
    manifest entries in-place where the ground truth differs (e.g. a file
    we thought was localised isn't there, or an HTML file still points to a
    CDN URL that should have been localised).
    """
    issues: list[DiagnosticIssue] = []
    dest = pathlib.Path(manifest.dest_path)

    accounted_remote_urls: set[str] = set()
    for e in manifest.entries:
        if e.remote_url:
            accounted_remote_urls.add(e.remote_url)
            accounted_remote_urls.add(os.path.basename(urlparse(e.remote_url).path))

    for entry in list(manifest.entries):
        if entry.local_path and entry.localized:
            p = pathlib.Path(entry.local_path)
            if not p.is_absolute():
                p = dest / entry.local_path
            if not p.is_file():
                entry.localized = False
                entry.mark_failed(f'expected localised file missing on disk: {entry.local_path}')
                issues.append(DiagnosticIssue(
                    severity='error',
                    category='inconsistency',
                    resource=entry.display_name,
                    message=(
                        f'Manifest says localised={entry.local_path!r} but file does not exist on disk; '
                        f'remote_url={entry.remote_url!r}'
                    ),
                    page=entry.page,
                    origin=entry.origin,
                    owner=entry.owner,
                ))
            elif entry.size_kb is None:
                entry.size_kb = _get_file_size_kb(str(p))

    html_entries = manifest.filter(category='html')
    for html_e in html_entries:
        if not html_e.local_path:
            continue
        html_path = dest / html_e.local_path
        if not html_path.is_file():
            continue
        try:
            html_text = html_path.read_text(encoding='utf-8')
        except OSError:
            continue
        urls_in_html = _extract_urls_from_html(html_text)
        for url in urls_in_html:
            if not url.startswith('http'):
                continue
            basename = os.path.basename(urlparse(url).path)
            matched: ManifestEntry | None = None
            for e in manifest.entries:
                if e.remote_url and (url in e.remote_url or e.remote_url in url):
                    matched = e
                    break
                if e.local_path and basename and basename in e.local_path:
                    matched = e
                    break
            if matched is None:
                issues.append(DiagnosticIssue(
                    severity='warning',
                    category='inconsistency',
                    resource=url,
                    message=(
                        f'Remote URL present in {html_e.local_path} but not tracked by manifest; '
                        f'may leak to network and break offline mode'
                    ),
                    page=html_e.page,
                    origin='html-scan',
                    owner=html_e.owner,
                ))
            elif matched.remote_url and matched.cache_policy == 'precache' and not matched.localized:
                issues.append(DiagnosticIssue(
                    severity='error',
                    category='inconsistency',
                    resource=matched.display_name,
                    message=(
                        f'HTML {html_e.local_path} still references remote URL {url!r} '
                        f'but manifest marks it cache_policy=precache; URL was not rewritten to local path'
                    ),
                    page=html_e.page,
                    origin=matched.origin,
                    owner=matched.owner,
                ))

    sw_entries = manifest.filter(category='service-worker', origin='app-worker')
    for sw_e in sw_entries:
        if not sw_e.local_path:
            continue
        sw_path = dest / sw_e.local_path
        if not sw_path.is_file():
            continue
        try:
            sw_text = sw_path.read_text(encoding='utf-8')
        except OSError:
            continue
        precache_match = re.search(r'const\s+PRE_CACHE\s*=\s*\[([^\]]+)\]', sw_text)
        sw_precache: set[str] = set()
        if precache_match:
            for m in re.findall(r"'([^']+)'", precache_match.group(1)):
                sw_precache.add(m)
        expected_precache = {
            e.local_path for e in manifest.entries
            if e.cache_policy == 'precache' and e.localized and e.local_path
        }
        missing_in_sw = expected_precache - sw_precache
        extra_in_sw = sw_precache - expected_precache
        for path in missing_in_sw:
            e = next((x for x in manifest.entries if x.local_path == path and x.cache_policy == 'precache'), None)
            issues.append(DiagnosticIssue(
                severity='error',
                category='inconsistency',
                resource=path,
                message=(
                    f'Manifest marks cache_policy=precache but {sw_e.local_path} '
                    f'does not include it in PRE_CACHE list'
                ),
                page=e.page if e else '__global__',
                origin=e.origin if e else 'manifest-summary',
                owner=e.owner if e else 'sw-sync',
            ))
        for path in extra_in_sw:
            issues.append(DiagnosticIssue(
                severity='info',
                category='inconsistency',
                resource=path,
                message=(
                    f'{sw_e.local_path} precaches {path!r} but no manifest entry '
                    f'has cache_policy=precache'
                ),
                page='__global__',
                origin='sw-scan',
                owner=sw_e.owner,
            ))
        for e in manifest.entries:
            if e.local_path and e.local_path in sw_precache and e.cache_policy != 'precache':
                e.cache_policy = 'precache'

    all_worker_entries = [
        e for e in manifest.entries
        if e.category == 'service-worker'
        or (e.local_path and (
            e.local_path.endswith('.js') and 'worker' in e.local_path.lower()
        ))
        or (e.local_path and e.local_path.endswith('.py') and e.origin == 'app-worker')
    ]
    for wk_e in all_worker_entries:
        if not wk_e.local_path:
            continue
        wk_path = dest / wk_e.local_path
        if not wk_path.is_file():
            continue
        try:
            wk_text = wk_path.read_text(encoding='utf-8')
        except OSError:
            continue

        found_urls = _scan_worker_for_static_urls(wk_text, wk_e.local_path)
        for url, ctx in found_urls:
            is_remote = _is_remote_url(url)
            matched: ManifestEntry | None = None
            for e in manifest.entries:
                if e.remote_url and (url in e.remote_url or e.remote_url in url):
                    matched = e
                    break
                if e.local_path:
                    url_basename = os.path.basename(urlparse(url).path)
                    lp_basename = os.path.basename(e.local_path)
                    if url_basename and lp_basename and url_basename == lp_basename:
                        matched = e
                        break
                    if e.local_path == url or url.endswith('/' + e.local_path):
                        matched = e
                        break
                if e.note and e.note in url:
                    matched = e
                    break

            if matched is None:
                cat = _url_to_category(url)
                issues.append(DiagnosticIssue(
                    severity='warning' if is_remote else 'info',
                    category='inconsistency',
                    resource=url,
                    message=(
                        f'Static resource {url!r} referenced by worker context `{ctx}` '
                        f'but not tracked by manifest; may leak to network'
                    ),
                    page=wk_e.page,
                    origin='worker-scan',
                    owner=ctx,
                ))
                untracked_entry = ManifestEntry(
                    page=wk_e.page,
                    category=cat,
                    origin='unknown',
                    owner=ctx,
                    local_path=None if is_remote else url,
                    remote_url=url if is_remote else None,
                    cache_policy='runtime-only',
                    localized=False,
                    note=f'Found in worker: {ctx}',
                    failure_reason='not tracked by manifest; discovered during worker scan',
                )
                manifest.add(untracked_entry)
                continue

            if is_remote and matched.remote_url and matched.cache_policy == 'precache' and not matched.localized:
                issues.append(DiagnosticIssue(
                    severity='error',
                    category='inconsistency',
                    resource=matched.display_name,
                    message=(
                        f'Worker {wk_e.local_path!r} still references remote URL {url!r} '
                        f'(via {ctx}); manifest marks cache_policy=precache but URL was not rewritten'
                    ),
                    page=wk_e.page,
                    origin=matched.origin,
                    owner=matched.owner,
                ))

            if is_remote and matched.remote_url and not matched.remote_url.startswith('emfs:'):
                if not matched.failure_reason:
                    matched.failure_reason = (
                        f'still referenced as remote URL in worker via {ctx}'
                    )

    return issues


_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']')
_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']')


def _extract_urls_from_html(html: str) -> list[str]:
    urls: list[str] = []
    for match in _HREF_RE.findall(html):
        urls.append(match)
    for match in _SRC_RE.findall(html):
        urls.append(match)
    return urls


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
) -> list[tuple[str, ManifestOrigin, str]]:
    """
    Make sense of python requirements for our Panel script.

    Returns a list of (req_str, origin, owner_detail) tuples ready to be
    turned into ManifestEntry records.
    """
    collected_requirements: list[tuple[str, ManifestOrigin, str]] = []

    if panel_version == 'local':
        panel_req = './' + str(PANEL_LOCAL_WHL.as_posix()).split('/')[-1]
        bokeh_req = './' + str(BOKEH_LOCAL_WHL.as_posix()).split('/')[-1]
    elif panel_version == 'auto':
        panel_req = PANEL_CDN_WHL
        bokeh_req = BOKEH_CDN_WHL
    else:
        panel_req = f'panel=={panel_version}'
        bokeh_req = f'bokeh=={BOKEH_VERSION}'

    collected_requirements.append((bokeh_req, 'wheel-panel-dep', 'bokeh runtime'))
    collected_requirements.append((panel_req, 'wheel-panel-dep', 'panel runtime'))
    if http_patch:
        collected_requirements.append(('pyodide-http', 'wheel-panel-dep', 'http patch for requests/urllib3'))

    requirements_root = os.getcwd()
    resolved_reqs: list[tuple[str, ManifestOrigin, str]]
    if requirements == 'auto':
        if hasattr(code, 'read'):
            source = code.read()
        else:
            path = pathlib.Path(code)
            application = build_single_handler_application(path.absolute())
            source = application._handlers[0]._runner.source
        detected = find_requirements(source)
        resolved_reqs = [
            (r, 'wheel-auto-detected', f'auto-detected from import in {os.path.basename(str(code))}')
            for r in detected
        ]
    elif isinstance(requirements, (str, os.PathLike)) and pathlib.Path(requirements).is_file():
        requirements_root = os.path.dirname(requirements)
        lines = (
            pathlib.Path(requirements).read_text(encoding='utf-8').splitlines()
        )
        resolved_reqs = [
            (r, 'wheel-requirements-file', f'from requirements file {os.path.basename(str(requirements))}')
            for r in lines
        ]
    elif isinstance(requirements, list):
        resolved_reqs = [
            (r, 'wheel-cli-list', 'from CLI --requirements list')
            for r in requirements
        ]
    else:
        raise ValueError(
            f'Requirements {requirements!r} could not be resolved. '
            'Provide a list of requirement specs, a path to a requirements.txt '
            'file that exists on disk or \'auto\' as a literal.'
        )

    for raw_req, origin, owner_detail in resolved_reqs:
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
                collected_requirements.append((req.url, origin, owner_detail))
            elif parsed_req.scheme in ('file', ''):
                check_path = parsed_req.path
                check_path = os.path.normpath(
                    os.path.join(requirements_root, check_path)
                )
                if os.path.exists(check_path):
                    collected_requirements.append((
                        f'file:{check_path}',
                        'wheel-local-file',
                        f'local wheel specified in {owner_detail}',
                    ))
                else:
                    raise ValueError(f'Could not verify path for {req}. Make sure the file is available if it is a local wheel.')
        else:
            collected_requirements.append((f'{req.name}{req.specifier}', origin, owner_detail))

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
    compiled: bool = True,
    dest_path: str | os.PathLike | None = None,
    asset_manifest: AssetManifest | None = None,
    page_name: str = '',
) -> tuple[str, str | None, ThemeInfo | None]:
    """
    Converts a Panel or Bokeh script to a standalone WASM Python
    application.  Populates ``asset_manifest`` (if provided) with full
    provenance records for wheels, JS/CSS, theme and runtime URLs.
    """
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

    page = page_name or app_name.replace('_', ' ')
    dest_p = pathlib.Path(dest_path) if dest_path else None

    if asset_manifest is not None:
        # Add Pyodide/PyScript runtime JS
        if runtime.startswith('pyscript'):
            asset_manifest.add(ManifestEntry(
                page=page, category='js', origin='panel-core',
                owner='pyscript-runtime',
                local_path=None,
                remote_url=f'https://pyscript.net/releases/{PYSCRIPT_VERSION}/core.js',
                cache_policy='cache-first',
                localized=False, size_kb=None,
            ))
            asset_manifest.add(ManifestEntry(
                page=page, category='css', origin='panel-core',
                owner='pyscript-runtime',
                local_path=None,
                remote_url=f'https://pyscript.net/releases/{PYSCRIPT_VERSION}/core.css',
                cache_policy='cache-first',
                localized=False, size_kb=None,
            ))
        else:
            py_url = PYODIDE_PYC_URL if compiled else PYODIDE_URL
            asset_manifest.add(ManifestEntry(
                page=page, category='js', origin='panel-core',
                owner='pyodide-runtime',
                local_path=None, remote_url=py_url,
                cache_policy='cache-first',
                localized=False, size_kb=None,
            ))
        # Collect pane/widget JS/CSS, theme, etc.
        _collect_resources_into_manifest(
            document, asset_manifest, page,
            roots=list(document.roots), dest_path=dest_p,
        )
        # Runtime URLs from source code
        source_name = os.path.basename(str(filename)) if not hasattr(filename, 'read') else 'app code'
        for entry in _extract_runtime_urls(source, page, source_name=source_name):
            asset_manifest.add(entry)

    # Execution
    post_code = POST_PYSCRIPT if runtime == 'pyscript' else POST
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

    template = document.template
    if template is None:
        template = BASE_TEMPLATE
    elif isinstance(template, str):
        template = get_env().from_string("{% extends base %}\n" + template)

    resources = Resources(mode='inline' if inline else 'cdn')
    css_resources += loading_resources(template, inline)
    with set_curdoc(document):
        bokeh_js, bokeh_css = bundle_resources(document.roots, resources)
    extra_js = [INIT_SERVICE_WORKER, bokeh_js] if manifest else [bokeh_js]
    bokeh_js = '\n'.join(js_resources+extra_js)
    bokeh_css = '\n'.join([bokeh_css]+css_resources)

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

    html = template.render(context)
    html = (html
        .replace('<body>', f'<body class="{LOADING_INDICATOR_CSS_CLASS} pn-{config.loading_spinner}">')
    )
    if runtime == 'pyscript-worker':
        html = (html
            .replace('<script type="text/javascript"', '<script type="text/javascript" crossorigin="anonymous"')
            .replace('<link rel="stylesheet"', '<link rel="stylesheet" crossorigin="anonymous"')
            .replace('<link rel="icon"', '<link rel="icon" crossorigin="anonymous"')
        )

    theme_info = _detect_theme(document)
    return html, web_worker, theme_info


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
    asset_manifest: AssetManifest | None = None,
):
    if dest_path is None:
        dest_path = pathlib.Path('./')
    elif not isinstance(dest_path, pathlib.PurePath):
        dest_path = pathlib.Path(dest_path)

    app_folder = os.path.dirname(app)
    app_name = '.'.join(os.path.basename(app).split('.')[:-1])
    page_name = app_name.replace('_', ' ')

    parsed_requirements = collect_python_requirements(
        app, requirements, panel_version=panel_version, http_patch=http_patch
    )
    parsed_requirements_rewritten: list[str] = []
    wheels2pack: dict[str | os.PathLike, str] = {}
    wheel_entries: list[ManifestEntry] = []

    for req_str, origin, owner_detail in parsed_requirements:
        req_as_url = urlparse(req_str)
        entry: ManifestEntry | None = None
        if req_as_url.scheme == 'file':
            wheel_name = os.path.basename(req_as_url.path)
            wheel_path = req_as_url.path
            if asset_manifest is not None:
                entry = asset_manifest.add(ManifestEntry(
                    page=page_name, category='wheel', origin=origin,
                    owner=owner_detail,
                    local_path=None, remote_url=None,
                    cache_policy='precache', localized=False,
                    note=f'local wheel: {wheel_name}',
                ))
                wheel_entries.append(entry)
            emfs_wheel_path = 'packed_wheels' + '/' + wheel_name
            parsed_requirements_rewritten.append(f'emfs:{emfs_wheel_path}')
            wheels2pack[req_as_url.path] = emfs_wheel_path
            if entry is not None and os.path.isfile(req_as_url.path):
                size_kb = _get_file_size_kb(req_as_url.path)
                entry.mark_localized(emfs_wheel_path, size_kb=size_kb)
                entry.note = f'packed into resources zip as {emfs_wheel_path}'
            elif entry is not None:
                entry.mark_failed(f'source wheel not found: {req_as_url.path}')
        else:
            is_local = req_as_url.scheme not in ('http', 'https')
            try:
                req_obj = Requirement(req_str.split('==')[0].split('>=')[0].split('<=')[0].split('~=')[0].split('!=')[0])
                wname = req_obj.name
            except Exception:
                wname = req_str
            if asset_manifest is not None:
                remote = req_str if not is_local else None
                local = req_str if is_local else None
                entry = asset_manifest.add(ManifestEntry(
                    page=page_name, category='wheel', origin=origin,
                    owner=owner_detail,
                    local_path=None, remote_url=remote,
                    cache_policy='cache-first', localized=False,
                    note=wname,
                ))
                wheel_entries.append(entry)
            parsed_requirements_rewritten.append(req_str)
            if entry is not None:
                if is_local and os.path.isfile(req_str):
                    size_kb = _get_file_size_kb(req_str)
                    entry.mark_localized(local, size_kb=size_kb)
                elif is_local:
                    entry.mark_failed(f'local wheel not found: {req_str}')
                else:
                    entry.mark_failed(f'remote CDN wheel: {req_str} (not bundled, fetched at runtime)')

    resources_validated: dict[str | os.PathLike, str] = {}
    user_resource_entries: list[ManifestEntry] = []
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
            if asset_manifest is not None:
                bad_entry = asset_manifest.add(ManifestEntry(
                    page=page_name, category='user-data', origin='user-cli-resource',
                    owner=f'CLI --resources: {os.path.basename(str(resourcepath))}',
                    local_path=str(resourcepath), remote_url=None,
                    cache_policy='cache-first', localized=False,
                ))
                bad_entry.mark_failed(f'resource outside app folder: {resourcepath}')
                user_resource_entries.append(bad_entry)
            else:
                raise ValueError('resources have to be in a folder rootable at the app-directory')

    app_resources = {**wheels2pack, **resources_validated}
    app_resources_packfile: str | None = None
    resources_zip_entry: ManifestEntry | None = None
    if app_resources:
        app_resources_packfile = f'{app_name}.resources.zip'
        try:
            pack_files(app_resources, os.path.join(dest_path, app_resources_packfile))
            zip_full_path = dest_path / app_resources_packfile
            if asset_manifest is not None:
                resources_zip_entry = asset_manifest.add(ManifestEntry(
                    page=page_name, category='user-data',
                    origin='user-cli-resource',
                    owner='packed resources zip (wheels + user files)',
                    local_path=None, remote_url=None,
                    cache_policy='precache', localized=False,
                ))
                if zip_full_path.is_file():
                    resources_zip_entry.mark_localized(
                        app_resources_packfile,
                        size_kb=_get_file_size_kb(str(zip_full_path)),
                    )
                else:
                    resources_zip_entry.mark_failed('pack_files did not produce zip')
        except Exception as e:
            if asset_manifest is not None and resources_zip_entry is None:
                resources_zip_entry = asset_manifest.add(ManifestEntry(
                    page=page_name, category='user-data',
                    origin='user-cli-resource',
                    owner='packed resources zip (wheels + user files)',
                    local_path=None, remote_url=None,
                    cache_policy='precache', localized=False,
                ))
            if resources_zip_entry is not None:
                resources_zip_entry.mark_failed(f'pack_files failed: {e}')

    for orig_path, rel_path in resources_validated.items():
        if asset_manifest is not None:
            size_kb = _get_file_size_kb(str(orig_path))
            entry = asset_manifest.add(ManifestEntry(
                page=page_name, category=_url_to_category(str(orig_path)),
                origin='user-cli-resource',
                owner=f'CLI --resources: {os.path.basename(str(orig_path))}',
                local_path=None, remote_url=None,
                cache_policy='cache-first', localized=False,
            ))
            user_resource_entries.append(entry)
            if app_resources_packfile:
                entry.mark_localized(rel_path, size_kb=size_kb)
                entry.note = f'packed inside {app_resources_packfile}'
            elif os.path.isfile(orig_path):
                entry.mark_localized(rel_path, size_kb=size_kb)
            else:
                entry.mark_failed(f'source not found: {orig_path}')

    try:
        with set_resource_mode('inline' if inline else 'cdn'):
            html, worker, theme_info = script_to_html(
                app,
                requirements=parsed_requirements_rewritten,
                app_resources=app_resources_packfile,
                runtime=runtime,
                prerender=prerender,
                manifest=manifest,
                panel_version=panel_version,
                inline=inline,
                compiled=compiled,
                local_prefix=local_prefix,
                dest_path=dest_path,
                asset_manifest=asset_manifest,
                page_name=page_name,
            )
    except KeyboardInterrupt:
        return
    except Exception as e:
        if asset_manifest is not None:
            for entry in wheel_entries + user_resource_entries:
                if not entry.localized and not entry.failure_reason:
                    entry.mark_failed(f'conversion failed before this resource was written: {e}')
        print(f'Failed to convert {app} to {runtime} target: {e}')
        return

    filename = f'{app_name}.html'
    html_entry: ManifestEntry | None = None

    with open(dest_path / filename, 'w', encoding='utf-8') as out:
        out.write(html)

    if asset_manifest is not None:
        html_entry = asset_manifest.add(ManifestEntry(
            page=page_name, category='html', origin='app-html',
            owner=filename,
            local_path=None, remote_url=None,
            cache_policy='precache', localized=False,
        ))
        full_html = dest_path / filename
        if full_html.is_file():
            html_entry.mark_localized(filename, size_kb=_get_file_size_kb(str(full_html)))
            html_entry.note = 'rendered HTML output'
        else:
            html_entry.mark_failed('HTML file not written to disk')

    worker_entry: ManifestEntry | None = None
    if 'worker' in runtime and worker:
        ext = 'py' if runtime.startswith('pyscript') else 'js'
        worker_filename = f'{app_name}.{ext}'
        with open(dest_path / worker_filename, 'w', encoding="utf-8") as out:
            out.write(worker)
        if asset_manifest is not None:
            worker_entry = asset_manifest.add(ManifestEntry(
                page=page_name, category='service-worker', origin='app-worker',
                owner=f'{runtime} worker',
                local_path=None, remote_url=None,
                cache_policy='cache-first', localized=False,
            ))
            full_worker = dest_path / worker_filename
            if full_worker.is_file():
                worker_entry.mark_localized(
                    worker_filename,
                    size_kb=_get_file_size_kb(str(full_worker)),
                )
                worker_entry.note = f'{runtime} web worker'
            else:
                worker_entry.mark_failed('worker file not written to disk')

    if verbose:
        print(f'Successfully converted {app} to {runtime} target and wrote output to {filename}.')
    entries = asset_manifest.entries if asset_manifest is not None else []
    return (page_name, filename, entries)


def _convert_process_pool(
    apps: Sequence[str | os.PathLike],
    dest_path: os.PathLike | str | None = None,
    max_workers: int = 4,
    requirements: list[str] | t.Literal['auto'] | os.PathLike = 'auto',
    **kwargs
):
    import multiprocessing as mp

    from concurrent.futures import ProcessPoolExecutor

    files: dict[str, str] = {}
    all_entries: list[ManifestEntry] = []
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
                    name, filename, entries = result
                    files[name] = filename
                    all_entries.extend(entries)
    return files, all_entries


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
    generate_assets_report: bool = True,
):
    """
    Parameters
    ----------
    apps: str | List[str]
        The filename(s) of the Panel/Bokeh application(s) to convert.
    dest_path: str | pathlib.Path
        The directory to write the converted application(s) to.
    title: str | None
        A title for the application(s).
    runtime: 'pyodide' | 'pyscript' | 'pyodide-worker'
        The runtime to use for running Python in the browser.
    requirements: 'auto' | List[str] | os.PathLike | Dict[str, ...]
        The list of requirements to include (in addition to Panel).
    prerender: bool
        Whether to pre-render the components.
    build_index: bool
        Whether to write an index page.
    build_pwa: bool
        Whether to write PWA files (manifest, service worker, icons).
    pwa_config: Dict[Any, Any]
        Configuration for the PWA.
    max_workers: int
        The maximum number of parallel workers
    panel_version: Literal['auto' | 'local'] | str
        The panel version to include.
    local_prefix: str
        Prefix for the path to serve local wheel files from.
    http_patch: bool
        Whether to patch the HTTP request stack.
    inline: bool
        Whether to inline resources.
    compiled: bool
        Whether to use the compiled and faster version of Pyodide.
    generate_assets_report: bool
        Whether to generate an asset manifest report (JSON + Markdown)
        and print diagnostics.
    """
    import datetime as _dt

    if isinstance(apps, (str, os.PathLike)):
        apps = [apps]
    if dest_path is None:
        dest_path = pathlib.Path('./')
    elif not isinstance(dest_path, pathlib.PurePath):
        dest_path = pathlib.Path(dest_path)
    dest_path.mkdir(parents=True, exist_ok=True)

    manifest = 'site.webmanifest' if build_pwa else None

    if isinstance(requirements, dict):
        app_requirements: dict = {}
        for app in apps:
            matches = [
                deps for name, deps in requirements.items()
                if str(app).endswith(name.replace(os.path.sep, '/'))
            ]
            app_requirements[app] = matches[0] if matches else 'auto'
    else:
        app_requirements = requirements

    asset_manifest: AssetManifest | None = None
    if generate_assets_report:
        asset_manifest = AssetManifest(
            generated_at=_dt.datetime.now().isoformat(),
            runtime=runtime,
            is_pwa=build_pwa,
            dest_path=str(dest_path),
        )

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
        'asset_manifest': asset_manifest,
    }

    files: dict[str, str] = {}

    if state._is_pyodide or generate_assets_report:
        for app in apps:
            if isinstance(app_requirements, dict):
                app_req = app_requirements.get(app, 'auto')
            else:
                app_req = app_requirements
            result = convert_app(
                app, dest_path, requirements=app_req,
                **{k: v for k, v in kwargs.items() if k != 'requirements'}
            )
            if result is not None:
                name, filename, _ = result
                files[name] = filename
    else:
        pool_kwargs = {k: v for k, v in kwargs.items() if k != 'asset_manifest'}
        files, _ = _convert_process_pool(
            apps, dest_path, max_workers=max_workers, **pool_kwargs  # type: ignore
        )

    if build_index and len(files) >= 1:
        index = make_index(files, manifest=build_pwa, title=title)
        index_path = dest_path / 'index.html'
        with open(index_path, 'w') as f:
            f.write(index)
        if asset_manifest is not None:
            index_entry = asset_manifest.add(ManifestEntry(
                page='__global__', category='html', origin='app-html',
                owner='index.html (app listing)',
                local_path=None, remote_url=None,
                cache_policy='precache', localized=False,
            ))
            if index_path.is_file():
                index_entry.mark_localized(
                    'index.html',
                    size_kb=_get_file_size_kb(str(index_path)),
                )
            else:
                index_entry.mark_failed('index.html was not written')
        if verbose:
            print('Successfully wrote index.html.')

    if not build_pwa and not generate_assets_report:
        return

    img_rel: list[str] = []

    if build_pwa:
        imgs_path = (dest_path / 'images')
        imgs_path.mkdir(exist_ok=True)
        img_rel = []
        for img in PWA_IMAGES:
            target = imgs_path / img.name
            with open(target, 'wb') as f:
                f.write(img.read_bytes())
            rel = f'images/{img.name}'
            img_rel.append(rel)
            if asset_manifest is not None:
                icon_entry = asset_manifest.add(ManifestEntry(
                    page='__global__', category='image', origin='pwa-icon',
                    owner=f'PWA icon: {img.name}',
                    local_path=None, remote_url=None,
                    cache_policy='precache', localized=False,
                ))
                if target.is_file():
                    icon_entry.mark_localized(
                        rel, size_kb=_get_file_size_kb(str(target)),
                    )
                else:
                    icon_entry.mark_failed(f'PWA icon not written: {img.name}')
        if verbose:
            print('Successfully wrote icons and images.')

        manifest_content = build_pwa_manifest(files, title=title, **pwa_config)
        webmanifest_path = dest_path / 'site.webmanifest'
        with open(webmanifest_path, 'w', encoding='utf-8') as f:
            f.write(manifest_content)
        if asset_manifest is not None:
            wm_entry = asset_manifest.add(ManifestEntry(
                page='__global__', category='user-data', origin='pwa-icon',
                owner='site.webmanifest (PWA manifest)',
                local_path=None, remote_url=None,
                cache_policy='precache', localized=False,
            ))
            if webmanifest_path.is_file():
                wm_entry.mark_localized(
                    'site.webmanifest',
                    size_kb=_get_file_size_kb(str(webmanifest_path)),
                )
            else:
                wm_entry.mark_failed('site.webmanifest was not written')
        if verbose:
            print('Successfully wrote site.manifest.')

        precache_list: list[str] = list(img_rel)
        if asset_manifest is not None:
            for entry in asset_manifest.entries:
                if entry.local_path and entry.cache_policy == 'precache' and entry.localized:
                    if entry.local_path not in precache_list:
                        precache_list.append(entry.local_path)
        else:
            precache_list = list(img_rel) + list(files.values())

        worker = SERVICE_WORKER_TEMPLATE.render(
            uuid=uuid.uuid4().hex,
            name=title or 'Panel Pyodide App',
            pre_cache=', '.join([repr(p) for p in precache_list])
        )
        sw_path = dest_path / 'serviceWorker.js'
        with open(sw_path, 'w', encoding='utf-8') as f:
            f.write(worker)
        if asset_manifest is not None:
            sw_entry = asset_manifest.add(ManifestEntry(
                page='__global__', category='service-worker', origin='app-worker',
                owner='serviceWorker.js (PWA cache manager)',
                local_path=None, remote_url=None,
                cache_policy='cache-first', localized=False,
                note=f'precache list: {len(precache_list)} files',
            ))
            if sw_path.is_file():
                sw_entry.mark_localized(
                    'serviceWorker.js',
                    size_kb=_get_file_size_kb(str(sw_path)),
                )
            else:
                sw_entry.mark_failed('serviceWorker.js was not written')
        if verbose:
            print('Successfully wrote serviceWorker.js.')

    if generate_assets_report and asset_manifest is not None:
        inconsistency_issues = _validate_manifest_vs_output(asset_manifest)
        base_issues = diagnose_assets(asset_manifest)
        issues = inconsistency_issues + base_issues
        json_path, md_path = write_assets_report(asset_manifest, issues, dest_path)
        if verbose:
            print(f'Successfully wrote asset manifest to {json_path.name} and {md_path.name}.')
            print_diagnostics(issues)
