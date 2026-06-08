from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import pathlib
import typing as t
import uuid

from bokeh.core.templates import get_env
from bokeh.document import Document

from .. import __version__, config
from ..util import base_version
from .resources import (
    BASE_TEMPLATE, CDN_DIST, CDN_ROOT, DIST_DIR, INDEX_TEMPLATE,
    _env as _pn_env, set_resource_mode,
)
from .state import set_curdoc, state

from ._convert import (
    AppConversionManifest,
    ConversionReport,
    ManifestCollector,
    ManifestPersistence,
    ManifestValidator,
    ReportRenderer,
)
from ._convert.manifest import Runtimes
from ._convert.collection import (
    PANEL_LOCAL_WHL,
    BOKEH_LOCAL_WHL,
    PANEL_CDN_WHL,
    BOKEH_CDN_WHL,
)
from ._convert.persistence import (
    PWA_MANIFEST_TEMPLATE,
    SERVICE_WORKER_TEMPLATE,
    WEB_WORKER_TEMPLATE,
    WORKER_HANDLER_TEMPLATE,
    PYODIDE_URL,
    PYODIDE_PYC_URL,
    PYSCRIPT_CSS,
    PYSCRIPT_CSS_OVERRIDES,
    PYSCRIPT_JS,
    PYODIDE_JS,
    PYODIDE_PYC_JS,
    PWA_IMAGES,
    ICON_DIR,
    PRE,
    POST,
    POST_PYSCRIPT,
    PYODIDE_SCRIPT,
    INIT_SERVICE_WORKER,
)

if t.TYPE_CHECKING:
    from collections.abc import Sequence

import bokeh
BOKEH_VERSION = base_version(bokeh.__version__)
PY_VERSION = base_version(__version__)
PYODIDE_VERSION = 'v0.29.3'
PYSCRIPT_VERSION = '2026.2.1'
WHL_PATH = DIST_DIR / 'wheels'
LOCAL_PREFIX = './'
MINIMUM_VERSIONS: dict[str, str] = {}

__all__ = [
    "BOKEH_CDN_WHL",
    "BOKEH_LOCAL_WHL",
    "BOKEH_VERSION",
    "CDN_DIST",
    "CDN_ROOT",
    "DIST_DIR",
    "ICON_DIR",
    "INIT_SERVICE_WORKER",
    "INDEX_TEMPLATE",
    "LOCAL_PREFIX",
    "PANEL_CDN_WHL",
    "PANEL_LOCAL_WHL",
    "POST",
    "POST_PYSCRIPT",
    "PRE",
    "PYODIDE_JS",
    "PYODIDE_PYC_JS",
    "PYODIDE_PYC_URL",
    "PYODIDE_SCRIPT",
    "PYODIDE_URL",
    "PYODIDE_VERSION",
    "PYSCRIPT_CSS",
    "PYSCRIPT_CSS_OVERRIDES",
    "PYSCRIPT_JS",
    "PYSCRIPT_VERSION",
    "PWA_IMAGES",
    "PWA_MANIFEST_TEMPLATE",
    "PY_VERSION",
    "Runtimes",
    "SERVICE_WORKER_TEMPLATE",
    "WEB_WORKER_TEMPLATE",
    "WHL_PATH",
    "WORKER_HANDLER_TEMPLATE",
    "collect_python_requirements",
    "convert_app",
    "convert_apps",
    "loading_resources",
    "make_index",
    "build_pwa_manifest",
    "pack_files",
    "script_to_html",
]


@dataclasses.dataclass
class DummyRequirement:
    url: str
    name: str = 'DUMMY'
    specifier: str = ''


def make_index(files, title=None, manifest=True):
    manifest_ref = 'site.webmanifest' if manifest else None
    favicon = 'images/favicon.ico' if manifest else None
    apple_icon = 'images/apple-touch-icon.png' if manifest else None
    items = {label: './'+os.path.basename(f) for label, f in sorted(files.items())}
    return INDEX_TEMPLATE.render(
        items=items, manifest=manifest_ref, apple_icon=apple_icon,
        favicon=favicon, title=title, PANEL_CDN=CDN_DIST,
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
        **kwargs,
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
    from ._convert.collection import ManifestCollector
    from .application import build_single_handler_application
    from .mime_render import find_requirements
    from urllib.parse import urlparse
    from packaging.requirements import Requirement

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
                    )
                else:
                    raise ValueError(f'Could not verify path for {req}. Make sure the file is available if it is a local wheel.')
        else:
            collected_requirements.append(f'{req.name}{req.specifier}')

    return collected_requirements


def pack_files(filemap: dict, destination: str | os.PathLike | t.IO):
    """
    Pack files into a zipfile for distribution
    """
    from zipfile import ZipFile
    with ZipFile(destination, 'w') as packfile:
        for fname, arcname in filemap.items():
            packfile.write(fname, arcname=arcname)


def loading_resources(template, inline) -> list[str]:
    import base64
    from .resources import loading_css
    css_resources = []
    if template in (BASE_TEMPLATE,):
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
) -> tuple[str, str | None]:
    """
    Converts a Panel or Bokeh script to a standalone WASM Python
    application.
    """
    import base64
    from html import escape
    from bokeh.application.handlers.code import CodeHandler
    from bokeh.core.json_encoder import serialize_json
    from bokeh.core.templates import FILE, MACROS
    from bokeh.embed.elements import script_for_render_items
    from bokeh.embed.util import RenderItem, standalone_docs_json_and_render_items
    from bokeh.embed.wrappers import wrap_in_script_tag
    from bokeh.util.serialization import make_id
    from ..util import base_version
    from .application import Application, build_single_handler_application
    from .document import MockSessionContext
    from .loading import LOADING_INDICATOR_CSS_CLASS
    from .resources import (
        BASE_TEMPLATE,
        CDN_DIST,
        Resources,
        bundle_resources,
        loading_css,
        set_resource_mode,
    )

    if hasattr(filename, 'read'):
        handler = CodeHandler(source=filename.read(), filename='convert.py')
        app_name = f'app-{str(uuid.uuid4())}'
        app = Application(handler)
    else:
        path = pathlib.Path(filename)
        app_name = '.'.join(path.name.split('.')[:-1])
        app = build_single_handler_application(str(path.absolute()))
    document = Document()
    document._session_context = lambda: MockSessionContext(document=document)
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
                'loading_spinner': config.loading_spinner,
            })
            web_worker = WEB_WORKER_TEMPLATE.render({
                'PYODIDE_URL': PYODIDE_PYC_URL if compiled else PYODIDE_URL,
                'data_archives': data_archives,
                'env_spec': env_spec,
                'code': code,
            })
            plot_script = wrap_in_script_tag(worker_handler)
        else:
            if js_resources == 'auto':
                js_resources = [PYODIDE_PYC_JS if compiled else PYODIDE_JS]
            script_template = _pn_env.from_string(PYODIDE_SCRIPT)
            plot_script = script_template.render({
                'data_archives': data_archives,
                'env_spec': env_spec,
                'code': code,
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
            use_for_title=False,
        )
        render_items = [render_item]

    template = document.template
    if template is None:
        template = BASE_TEMPLATE
    elif isinstance(template, str):
        template = get_env().from_string("{% extends base %}\n" + template)

    resources = Resources(mode='inline' if inline else 'cdn')
    css_resources = css_resources or []
    css_resources += loading_resources(template, inline)
    with set_curdoc(document):
        bokeh_js, bokeh_css = bundle_resources(document.roots, resources)
    extra_js = [INIT_SERVICE_WORKER, bokeh_js] if manifest else [bokeh_js]
    bokeh_js = '\n'.join(js_resources + extra_js) if isinstance(js_resources, list) else extra_js[0]
    bokeh_css = '\n'.join([bokeh_css] + list(css_resources))

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
        dist_url=CDN_DIST,
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
    return html, web_worker


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

    build_pwa = manifest is not None

    collector = ManifestCollector(
        app,
        dest_path,
        runtime,
        requirements=requirements,
        resources=resources,
        panel_version=panel_version,
        prerender=prerender,
        inline=inline,
        compiled=compiled,
        http_patch=http_patch,
        build_pwa=build_pwa,
    )
    app_manifest = collector.build()

    persistence = ManifestPersistence(
        app_manifest,
        panel_version=panel_version,
        local_prefix=local_prefix,
    )

    try:
        with set_resource_mode('inline' if inline else 'cdn'):
            persistence.persist_all()
    except KeyboardInterrupt:
        return None
    except Exception as e:
        if verbose:
            print(f'Failed to convert {app} to {runtime} target: {e}')
        return None

    validator = ManifestValidator(app_manifest)
    validator.validate_all()

    renderer = ReportRenderer(verbose=verbose)
    app_report = renderer.build_app_report(app_manifest)
    renderer.print_app_summary(app_report)

    if not app_report.success:
        return None

    filename = f'{app_manifest.app_name}.html'
    return (app_manifest.app_name.replace('_', ' '), filename)


def _convert_process_pool(
    apps: Sequence[str | os.PathLike],
    dest_path: os.PathLike | str | None = None,
    max_workers: int = 4,
    requirements: list[str] | t.Literal['auto'] | os.PathLike = 'auto',
    **kwargs,
):
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    files = {}
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
                    name, filename = result
                    files[name] = filename
    return files


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

    manifest_ref = 'site.webmanifest' if build_pwa else None

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
        'manifest': manifest_ref,
        'panel_version': panel_version,
        'http_patch': http_patch,
        'inline': inline,
        'verbose': verbose,
        'compiled': compiled,
        'local_prefix': local_prefix,
    }

    if state._is_pyodide:
        files = {
            app: convert_app(app, dest_path, **kwargs)  # type: ignore
            for app in apps
        }
    else:
        files = _convert_process_pool(
            apps, dest_path, max_workers=max_workers, **kwargs  # type: ignore
        )

    files = {k: v for k, v in files.items() if v is not None}

    if build_index and len(files) >= 1:
        index = make_index(files, manifest=build_pwa, title=title)
        with open(dest_path / 'index.html', 'w') as f:
            f.write(index)
        if verbose:
            print('Successfully wrote index.html.')

    if not build_pwa:
        return

    imgs_path = (dest_path / 'images')
    imgs_path.mkdir(exist_ok=True)
    for img in PWA_IMAGES:
        with open(imgs_path / img.name, 'wb') as f:
            f.write(img.read_bytes())
    if verbose:
        print('Successfully wrote icons and images.')

    manifest_content = build_pwa_manifest(files, title=title, **pwa_config)
    with open(dest_path / 'site.webmanifest', 'w', encoding='utf-8') as f:
        f.write(manifest_content)
    if verbose:
        print('Successfully wrote site.manifest.')

    worker = SERVICE_WORKER_TEMPLATE.render(
        uuid=uuid.uuid4().hex,
        name=title or 'Panel Pyodide App',
        pre_cache=', '.join([repr(f'images/{img.name}') for img in PWA_IMAGES])
    )
    with open(dest_path / 'serviceWorker.js', 'w', encoding='utf-8') as f:
        f.write(worker)
    if verbose:
        print('Successfully wrote serviceWorker.js.')
