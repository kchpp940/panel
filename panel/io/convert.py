from __future__ import annotations

import concurrent.futures
import os
import pathlib
import typing as t
import uuid

from ._convert import (
    BOKEH_CDN_WHL,
    BOKEH_LOCAL_WHL,
    BOKEH_VERSION,
    CDN_DIST,
    CDN_ROOT,
    DIST_DIR,
    ICON_DIR,
    INDEX_TEMPLATE,
    INIT_SERVICE_WORKER,
    LOCAL_PREFIX,
    PANEL_CDN_WHL,
    PANEL_LOCAL_WHL,
    POST,
    POST_PYSCRIPT,
    PRE,
    PYODIDE_JS,
    PYODIDE_PYC_JS,
    PYODIDE_PYC_URL,
    PYODIDE_SCRIPT,
    PYODIDE_URL,
    PYODIDE_VERSION,
    PYSCRIPT_CSS,
    PYSCRIPT_CSS_OVERRIDES,
    PYSCRIPT_JS,
    PYSCRIPT_VERSION,
    PWA_IMAGES,
    PWA_MANIFEST_TEMPLATE,
    PY_VERSION,
    SERVICE_WORKER_TEMPLATE,
    WEB_WORKER_TEMPLATE,
    WHL_PATH,
    WORKER_HANDLER_TEMPLATE,
    ManifestCollector,
    ManifestPersistence,
    ManifestValidator,
    ReportRenderer,
    Runtimes,
    build_pwa_manifest,
    collect_python_requirements,
    loading_resources,
    make_index,
    pack_files,
    script_to_html,
)
from ._convert.collection import DummyRequirement
from .resources import set_resource_mode
from .state import state

if t.TYPE_CHECKING:
    from collections.abc import Sequence

MINIMUM_VERSIONS: dict[str, str] = {}

__all__ = [
    "BOKEH_CDN_WHL",
    "BOKEH_LOCAL_WHL",
    "BOKEH_VERSION",
    "CDN_DIST",
    "CDN_ROOT",
    "DIST_DIR",
    "DummyRequirement",
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
    "build_pwa_manifest",
    "collect_python_requirements",
    "convert_app",
    "convert_apps",
    "loading_resources",
    "make_index",
    "pack_files",
    "script_to_html",
]


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
