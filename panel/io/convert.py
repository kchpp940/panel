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

_URL_RE = re.compile(r'https?://[^\s"\'<>)]+')


@dataclasses.dataclass
class WheelInfo:
    name: str
    source: str
    size_kb: float | None = None
    local: bool = False


@dataclasses.dataclass
class ResourceInfo:
    path: str
    type: t.Literal['js', 'css', 'image', 'font', 'other']
    source: t.Literal['cdn', 'local', 'inline', 'external']
    size_kb: float | None = None
    referenced_by: str = ''


@dataclasses.dataclass
class ThemeInfo:
    name: str
    css_files: list[str]
    bokeh_theme: str | None = None


@dataclasses.dataclass
class SWCacheStrategy:
    pre_cache_files: list[str]
    strategy: t.Literal['cache-first', 'network-first', 'stale-while-revalidate'] = 'cache-first'
    runtime_caching: bool = True


@dataclasses.dataclass
class PageAssets:
    page_name: str
    html_file: str
    wheels: list[WheelInfo] = dataclasses.field(default_factory=list)
    js_resources: list[ResourceInfo] = dataclasses.field(default_factory=list)
    css_resources: list[ResourceInfo] = dataclasses.field(default_factory=list)
    theme: ThemeInfo | None = None
    user_resources: list[str] = dataclasses.field(default_factory=list)
    runtime_urls: list[str] = dataclasses.field(default_factory=list)
    total_size_kb: float = 0.0


@dataclasses.dataclass
class DiagnosticIssue:
    severity: t.Literal['error', 'warning', 'info']
    category: t.Literal['missing', 'duplicate', 'non-localized', 'size-warning']
    resource: str
    message: str
    source: str = ''


@dataclasses.dataclass
class AssetsReport:
    generated_at: str
    runtime: str
    is_pwa: bool
    pages: list[PageAssets] = dataclasses.field(default_factory=list)
    sw_cache: SWCacheStrategy | None = None
    issues: list[DiagnosticIssue] = dataclasses.field(default_factory=list)
    total_size_kb: float = 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_markdown(self) -> str:
        lines = [
            '# Panel Convert Assets Report',
            '',
            f'- **Generated at**: {self.generated_at}',
            f'- **Runtime**: {self.runtime}',
            f'- **PWA enabled**: {self.is_pwa}',
            f'- **Total estimated size**: {self.total_size_kb:.1f} KB',
            '',
        ]
        if self.sw_cache:
            lines.extend([
                '## Service Worker Cache Strategy',
                '',
                f'- **Strategy**: {self.sw_cache.strategy}',
                f'- **Runtime caching**: {self.sw_cache.runtime_caching}',
                f'- **Pre-cached files** ({len(self.sw_cache.pre_cache_files)}):',
            ])
            for f in self.sw_cache.pre_cache_files:
                lines.append(f'  - `{f}`')
            lines.append('')
        for page in self.pages:
            lines.extend([
                f'## Page: {page.page_name}',
                '',
                f'- **HTML file**: `{page.html_file}`',
                f'- **Estimated size**: {page.total_size_kb:.1f} KB',
                '',
            ])
            if page.wheels:
                lines.append(f'### Python Wheels ({len(page.wheels)})')
                lines.append('')
                lines.append('| Name | Source | Size (KB) | Local |')
                lines.append('|------|--------|-----------|-------|')
                for w in page.wheels:
                    size = f'{w.size_kb:.1f}' if w.size_kb else 'N/A'
                    lines.append(f'| {w.name} | {w.source} | {size} | {w.local} |')
                lines.append('')
            if page.js_resources:
                lines.append(f'### JavaScript Resources ({len(page.js_resources)})')
                lines.append('')
                lines.append('| Path | Source | Size (KB) |')
                lines.append('|------|--------|-----------|')
                for r in page.js_resources:
                    size = f'{r.size_kb:.1f}' if r.size_kb else 'N/A'
                    lines.append(f'| {r.path} | {r.source} | {size} |')
                lines.append('')
            if page.css_resources:
                lines.append(f'### CSS Resources ({len(page.css_resources)})')
                lines.append('')
                lines.append('| Path | Source | Size (KB) |')
                lines.append('|------|--------|-----------|')
                for r in page.css_resources:
                    size = f'{r.size_kb:.1f}' if r.size_kb else 'N/A'
                    lines.append(f'| {r.path} | {r.source} | {size} |')
                lines.append('')
            if page.theme:
                lines.append(f'### Theme: {page.theme.name}')
                lines.append('')
                if page.theme.bokeh_theme:
                    lines.append(f'- **Bokeh theme**: {page.theme.bokeh_theme}')
                lines.append('- **CSS files**:')
                for css in page.theme.css_files:
                    lines.append(f'  - `{css}`')
                lines.append('')
            if page.user_resources:
                lines.append(f'### User Resources ({len(page.user_resources)})')
                lines.append('')
                for r in page.user_resources:
                    lines.append(f'- `{r}`')
                lines.append('')
            if page.runtime_urls:
                lines.append(f'### Runtime Network URLs ({len(page.runtime_urls)})')
                lines.append('')
                lines.append('> These URLs are referenced in the code and may be fetched at runtime.')
                lines.append('> They must be reachable or cached for the app to work offline.')
                lines.append('')
                for url in page.runtime_urls:
                    lines.append(f'- `{url}`')
                lines.append('')
        if self.issues:
            lines.extend([
                '## Diagnostics',
                '',
            ])
            by_severity: dict[str, list[DiagnosticIssue]] = {}
            for issue in self.issues:
                by_severity.setdefault(issue.severity, []).append(issue)
            for severity in ('error', 'warning', 'info'):
                if severity in by_severity:
                    sev_label = severity.upper()
                    lines.append(f'### {sev_label} ({len(by_severity[severity])})')
                    lines.append('')
                    for issue in by_severity[severity]:
                        src = f' (source: `{issue.source}`)' if issue.source else ''
                        lines.append(f'- **[{issue.category}]** {issue.resource}: {issue.message}{src}')
                    lines.append('')
        return '\n'.join(lines)


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


def _extract_runtime_urls(code: str) -> list[str]:
    urls = set()
    for match in _URL_RE.findall(code):
        if not match.endswith(('.whl', '.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico')):
            urls.add(match.rstrip('.,;:'))
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in _URL_RE.findall(node.value):
                    if not match.endswith(('.whl', '.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico')):
                        urls.add(match.rstrip('.,;:'))
    except SyntaxError:
        pass
    return sorted(urls)


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


def diagnose_assets(
    pages: list[PageAssets],
    dest_path: pathlib.Path,
) -> list[DiagnosticIssue]:
    issues: list[DiagnosticIssue] = []
    all_resources: list[tuple[str, str]] = []
    for page in pages:
        for w in page.wheels:
            all_resources.append((w.source, page.page_name))
            if not w.local:
                parsed = urlparse(w.source)
                if parsed.scheme in ('http', 'https'):
                    issues.append(DiagnosticIssue(
                        severity='warning',
                        category='non-localized',
                        resource=w.name,
                        message=f'Wheel loaded from remote URL {w.source}, will not work fully offline',
                        source=page.page_name,
                    ))
            else:
                local_path = pathlib.Path(w.source.replace('file:', ''))
                if not local_path.is_file():
                    issues.append(DiagnosticIssue(
                        severity='error',
                        category='missing',
                        resource=w.name,
                        message=f'Local wheel not found at {w.source}',
                        source=page.page_name,
                    ))
        for rtype in ('js_resources', 'css_resources'):
            for r in getattr(page, rtype):
                all_resources.append((r.path, page.page_name))
                if r.source in ('cdn', 'external'):
                    issues.append(DiagnosticIssue(
                        severity='warning',
                        category='non-localized',
                        resource=r.path,
                        message=f'{rtype.split("_")[0].upper()} resource loaded from remote URL, may fail offline',
                        source=page.page_name,
                    ))
                elif r.source == 'local':
                    local_path = pathlib.Path(r.path)
                    if not local_path.is_absolute():
                        local_path = dest_path / local_path
                    if not local_path.is_file():
                        issues.append(DiagnosticIssue(
                            severity='error',
                            category='missing',
                            resource=r.path,
                            message=f'Local resource file not found',
                            source=page.page_name,
                        ))
        for ur in page.user_resources:
            all_resources.append((ur, page.page_name))
            local_path = pathlib.Path(ur)
            if not local_path.is_absolute():
                local_path = dest_path / local_path
            if not local_path.is_file():
                issues.append(DiagnosticIssue(
                    severity='error',
                    category='missing',
                    resource=ur,
                    message='User resource file not found in output directory',
                    source=page.page_name,
                ))
    counts = Counter(path for path, _ in all_resources)
    for path, count in counts.items():
        if count > 1:
            pages_with = sorted(set(pg for p, pg in all_resources if p == path))
            issues.append(DiagnosticIssue(
                severity='info',
                category='duplicate',
                resource=path,
                message=f'Resource included {count} times across pages: {", ".join(pages_with)}',
                source=', '.join(pages_with),
            ))
    for page in pages:
        if page.total_size_kb > 5000:
            issues.append(DiagnosticIssue(
                severity='warning',
                category='size-warning',
                resource=page.page_name,
                message=f'Page assets exceed 5MB ({page.total_size_kb:.1f} KB), consider optimizing wheels and resources',
                source=page.page_name,
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
            src = f' [{issue.source}]' if issue.source else ''
            print(f'   [{issue.category}] {issue.resource}: {issue.message}{src}')
    print()


def write_assets_report(
    report: AssetsReport,
    dest_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    json_path = dest_path / 'assets_report.json'
    md_path = dest_path / 'assets_report.md'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(report.to_dict(), f, indent=2, default=str)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(report.to_markdown())
    return json_path, md_path


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
) -> tuple[str, str | None, list[ResourceInfo], list[ResourceInfo], ThemeInfo | None, list[str]]:
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
    css_resources += loading_resources(template, inline)
    with set_curdoc(document):
        bokeh_js, bokeh_css = bundle_resources(document.roots, resources)
    extra_js = [INIT_SERVICE_WORKER, bokeh_js] if manifest else [bokeh_js]
    bokeh_js = '\n'.join(js_resources+extra_js)
    bokeh_css = '\n'.join([bokeh_css]+css_resources)

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
    js_infos: list[ResourceInfo] = []
    css_infos: list[ResourceInfo] = []
    all_urls = _extract_urls_from_html(bokeh_js) + _extract_urls_from_html(bokeh_css)
    seen_urls = set()
    for url in all_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        rtype = _classify_resource_type(url)
        rsrc = _classify_resource_source(url)
        size_kb = _get_file_size_kb(url)
        info = ResourceInfo(
            path=url,
            type=rtype,
            source=rsrc,
            size_kb=size_kb,
        )
        if rtype == 'js':
            js_infos.append(info)
        elif rtype == 'css':
            css_infos.append(info)
        else:
            if rsrc in ('cdn', 'external'):
                js_infos.append(info)
    theme_info = _detect_theme(document)
    runtime_urls = _extract_runtime_urls(source)
    return html, web_worker, js_infos, css_infos, theme_info, runtime_urls


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
):
    if dest_path is None:
        dest_path = pathlib.Path('./')
    elif not isinstance(dest_path, pathlib.PurePath):
        dest_path = pathlib.Path(dest_path)

    app_folder = os.path.dirname(app)
    app_name = '.'.join(os.path.basename(app).split('.')[:-1])

    # Obtain source
    parsed_requirements = collect_python_requirements(
        app, requirements, panel_version=panel_version, http_patch=http_patch
    )
    # prepare wheels to be available via emscripten MEMFS
    parsed_requirements_rewritten = []
    wheels2pack: dict[str | os.PathLike, str] = {}
    wheel_infos: list[WheelInfo] = []

    for req in parsed_requirements:
        try:
            req_as_url = urlparse(req)
            if req_as_url.scheme == 'file':
                wheel_name = os.path.basename(req_as_url.path)
                wheel_path = req_as_url.path
                size_kb = _get_file_size_kb(wheel_path)
                wheel_infos.append(WheelInfo(
                    name=wheel_name,
                    source=wheel_path,
                    size_kb=size_kb,
                    local=True,
                ))
                emfs_wheel_path = 'packed_wheels' + '/' + wheel_name
                parsed_requirements_rewritten.append(f'emfs:{emfs_wheel_path}')
                wheels2pack[req_as_url.path] = emfs_wheel_path
            else:
                is_local = req_as_url.scheme not in ('http', 'https')
                size_kb = _get_file_size_kb(req) if is_local else None
                try:
                    req_obj = Requirement(req.split('==')[0].split('>=')[0].split('<=')[0].split('~=')[0].split('!=')[0])
                    wname = req_obj.name
                except Exception:
                    wname = req
                wheel_infos.append(WheelInfo(
                    name=wname,
                    source=req,
                    size_kb=size_kb,
                    local=is_local,
                ))
                parsed_requirements_rewritten.append(req)
        except ValueError:
            # no url, so must be a properly formatted requirement
            try:
                req_obj = Requirement(req.split('==')[0].split('>=')[0].split('<=')[0].split('~=')[0].split('!=')[0])
                wname = req_obj.name
            except Exception:
                wname = req
            wheel_infos.append(WheelInfo(
                name=wname,
                source=req,
                local=False,
            ))
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
    js_infos: list[ResourceInfo] = []
    css_infos: list[ResourceInfo] = []
    theme_info: ThemeInfo | None = None
    runtime_urls: list[str] = []
    try:
        with set_resource_mode('inline' if inline else 'cdn'):
            html, worker, js_infos, css_infos, theme_info, runtime_urls = script_to_html(
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

    # write out the app
    filename = f'{app_name}.html'

    with open(dest_path / filename, 'w', encoding='utf-8') as out:
        out.write(html)
    if 'worker' in runtime and worker:
        ext = 'py' if runtime.startswith('pyscript') else 'js'
        with open(dest_path / f'{app_name}.{ext}', 'w', encoding="utf-8") as out:
            out.write(worker)

    user_resources = list(resources_validated.values())
    if app_resources_packfile:
        user_resources.append(app_resources_packfile)

    total_size = 0.0
    for w in wheel_infos:
        if w.size_kb:
            total_size += w.size_kb
    for r in js_infos + css_infos:
        if r.size_kb:
            total_size += r.size_kb

    page_assets = PageAssets(
        page_name=app_name.replace('_', ' '),
        html_file=filename,
        wheels=wheel_infos,
        js_resources=js_infos,
        css_resources=css_infos,
        theme=theme_info,
        user_resources=user_resources,
        runtime_urls=runtime_urls,
        total_size_kb=total_size,
    )

    if verbose:
        print(f'Successfully converted {app} to {runtime} target and wrote output to {filename}.')
    return (app_name.replace('_', ' '), filename, page_assets)


def _convert_process_pool(
    apps: Sequence[str | os.PathLike],
    dest_path: os.PathLike | str | None = None,
    max_workers: int = 4,
    requirements: list[str] | t.Literal['auto'] | os.PathLike = 'auto',
    **kwargs
):
    import multiprocessing as mp

    from concurrent.futures import ProcessPoolExecutor

    files = {}
    page_assets: list[PageAssets] = []
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
                    name, filename, pa = result
                    files[name] = filename
                    page_assets.append(pa)
    return files, page_assets


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
    generate_assets_report: bool
        Whether to generate an assets report (assets_report.json and
        assets_report.md) and print diagnostic information about
        missing, duplicate and non-localized resources.
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

    all_page_assets: list[PageAssets] = []
    if state._is_pyodide:
        files = {}
        for app in apps:
            result = convert_app(app, dest_path, **kwargs)  # type: ignore
            if result is not None:
                name, filename, pa = result
                files[name] = filename
                all_page_assets.append(pa)
    else:
        files, all_page_assets = _convert_process_pool(
            apps, dest_path, max_workers=max_workers, **kwargs  # type: ignore
        )

    if build_index and len(files) >= 1:
        index = make_index(files, manifest=build_pwa, title=title)
        with open(dest_path / 'index.html', 'w') as f:
            f.write(index)
        if verbose:
            print('Successfully wrote index.html.')

    if not build_pwa and not generate_assets_report:
        return

    sw_cache: SWCacheStrategy | None = None
    img_rel: list[str] = []

    if build_pwa:
        # Write icons
        imgs_path = (dest_path / 'images')
        imgs_path.mkdir(exist_ok=True)
        img_rel = []
        for img in PWA_IMAGES:
            with open(imgs_path / img.name, 'wb') as f:
                f.write(img.read_bytes())
            img_rel.append(f'images/{img.name}')
        if verbose:
            print('Successfully wrote icons and images.')

        # Write manifest
        manifest_content = build_pwa_manifest(files, title=title, **pwa_config)
        with open(dest_path / 'site.webmanifest', 'w', encoding='utf-8') as f:
            f.write(manifest_content)
        if verbose:
            print('Successfully wrote site.manifest.')

        # Write service worker
        worker = SERVICE_WORKER_TEMPLATE.render(
            uuid=uuid.uuid4().hex,
            name=title or 'Panel Pyodide App',
            pre_cache=', '.join([repr(p) for p in img_rel])
        )
        with open(dest_path / 'serviceWorker.js', 'w', encoding='utf-8') as f:
            f.write(worker)
        if verbose:
            print('Successfully wrote serviceWorker.js.')

        sw_cache = SWCacheStrategy(
            pre_cache_files=img_rel,
            strategy='cache-first',
            runtime_caching=True,
        )

    if generate_assets_report:
        issues: list[DiagnosticIssue] = []
        if all_page_assets:
            issues = diagnose_assets(all_page_assets, dest_path)
        total_size = sum(pa.total_size_kb for pa in all_page_assets)
        report = AssetsReport(
            generated_at=_dt.datetime.now().isoformat(),
            runtime=runtime,
            is_pwa=build_pwa,
            pages=all_page_assets,
            sw_cache=sw_cache,
            issues=issues,
            total_size_kb=total_size,
        )
        json_path, md_path = write_assets_report(report, dest_path)
        if verbose:
            print(f'Successfully wrote assets report to {json_path.name} and {md_path.name}.')
            print_diagnostics(issues)
