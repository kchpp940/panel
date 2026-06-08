from __future__ import annotations

import dataclasses
import os
import pathlib
import typing as t
from urllib.parse import urlparse

import bokeh
from packaging.requirements import Requirement

from ..application import build_single_handler_application
from ..mime_render import find_requirements
from ..resources import (
    CDN_DIST,
    CDN_ROOT,
    DIST_DIR,
)
from ...util import base_version
from ... import __version__
from .manifest import (
    AppConversionManifest,
    AssetStatus,
    IssueSeverity,
    ResourceAsset,
    Runtimes,
    WheelAsset,
    WorkerAsset,
    WorkerType,
)

if t.TYPE_CHECKING:
    from collections.abc import Sequence

BOKEH_VERSION = base_version(bokeh.__version__)
PY_VERSION = base_version(__version__)
WHL_PATH = DIST_DIR / 'wheels'
PANEL_LOCAL_WHL = WHL_PATH / f'panel-{__version__.replace("-dirty", "")}-py3-none-any.whl'
BOKEH_LOCAL_WHL = WHL_PATH / f'bokeh-{BOKEH_VERSION}-py3-none-any.whl'
PANEL_CDN_WHL = f'{CDN_DIST}wheels/panel-{PY_VERSION}-py3-none-any.whl'
BOKEH_CDN_WHL = f'{CDN_ROOT}wheels/bokeh-{BOKEH_VERSION}-py3-none-any.whl'


@dataclasses.dataclass
class DummyRequirement:
    url: str
    name: str = 'DUMMY'
    specifier: str = ''


def _resolve_panel_bokeh_reqs(
    panel_version: t.Literal['auto', 'local'] | str,
    bokeh_version: str = BOKEH_VERSION,
) -> tuple[str, str]:
    if panel_version == 'local':
        panel_req = './' + str(PANEL_LOCAL_WHL.as_posix()).split('/')[-1]
        bokeh_req = './' + str(BOKEH_LOCAL_WHL.as_posix()).split('/')[-1]
    elif panel_version == 'auto':
        panel_req = PANEL_CDN_WHL
        bokeh_req = BOKEH_CDN_WHL
    else:
        panel_req = f'panel=={panel_version}'
        bokeh_req = f'bokeh=={bokeh_version}'
    return panel_req, bokeh_req


def _extract_source(
    code: str | os.PathLike | t.IO,
) -> tuple[str, pathlib.Path | None]:
    if hasattr(code, 'read'):
        source = code.read()
        return source, None
    else:
        path = pathlib.Path(code)
        application = build_single_handler_application(path.absolute())
        source = application._handlers[0]._runner.source
        return source, path


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
    code: str | os.PathLike | IO,
        The filename of the Panel/Bokeh application to convert,
        or a file-like object with a .read() method.
    requirements: list[str] | os.PathLike | Literal['auto']
        The list of requirements to include (in addition to Panel).
    panel_version: Literal['auto', 'local'] | str
        The panel release version to use in the exported HTML.
    http_patch: bool
        Whether to patch the HTTP request stack with the pyodide-http library
        to allow urllib3 and requests to work.
    """
    panel_req, bokeh_req = _resolve_panel_bokeh_reqs(panel_version)
    collected_requirements = [bokeh_req, panel_req]
    if http_patch:
        collected_requirements.append('pyodide-http')

    requirements_root = os.getcwd()
    resolved_reqs: list[str]
    if requirements == 'auto':
        source, _ = _extract_source(code)
        resolved_reqs = find_requirements(source)
    elif (
        isinstance(requirements, (str, os.PathLike))
        and pathlib.Path(requirements).is_file()
    ):
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
                    collected_requirements.append(f'file:{check_path}')
                else:
                    raise ValueError(
                        f'Could not verify path for {req}. '
                        'Make sure the file is available if it is a local wheel.'
                    )
        else:
            collected_requirements.append(f'{req.name}{req.specifier}')

    return collected_requirements


class ManifestCollector:
    def __init__(
        self,
        app: str | os.PathLike,
        dest_path: str | os.PathLike,
        runtime: Runtimes,
        *,
        requirements: list[str] | t.Literal['auto'] | os.PathLike = 'auto',
        resources: Sequence[str | os.PathLike] | None = None,
        panel_version: t.Literal['auto', 'local'] | str = 'auto',
        prerender: bool = True,
        inline: bool = False,
        compiled: bool = False,
        http_patch: bool = True,
        build_pwa: bool = False,
    ) -> None:
        self._app = app
        self._app_path = pathlib.Path(app) if not hasattr(app, 'read') else pathlib.Path('./')
        self._dest_path = pathlib.Path(dest_path)
        self._runtime = runtime
        self._requirements_input = requirements
        self._resources_input = list(resources) if resources else []
        self._panel_version = panel_version
        self._prerender = prerender
        self._inline = inline
        self._compiled = compiled
        self._http_patch = http_patch
        self._build_pwa = build_pwa

        self._app_folder = os.path.dirname(self._app_path)
        if hasattr(app, 'read'):
            self._app_name = f'app-{str(__import__("uuid").uuid4())}'
        else:
            self._app_name = '.'.join(os.path.basename(self._app_path).split('.')[:-1])

    def build(self) -> AppConversionManifest:
        manifest = AppConversionManifest(
            app_name=self._app_name,
            app_path=self._app_path,
            dest_path=self._dest_path,
            runtime=self._runtime,
            prerender=self._prerender,
            inline=self._inline,
            compiled=self._compiled,
            http_patch=self._http_patch,
            build_pwa=self._build_pwa,
        )

        self._collect_requirements(manifest)
        self._collect_wheels(manifest)
        self._collect_resources(manifest)
        self._collect_workers(manifest)

        return manifest

    def _collect_requirements(self, manifest: AppConversionManifest) -> None:
        try:
            collected = collect_python_requirements(
                self._app,
                requirements=self._requirements_input,
                panel_version=self._panel_version,
                http_patch=self._http_patch,
            )
        except Exception as exc:
            manifest.add_issue(
                IssueSeverity.ERROR,
                f"Failed to resolve requirements: {exc}",
                location="requirements",
            )
            return

        manifest.requirements = collected

    def _collect_wheels(self, manifest: AppConversionManifest) -> None:
        for req in manifest.requirements:
            try:
                req_as_url = urlparse(req)
            except ValueError:
                continue

            if req_as_url.scheme != 'file':
                continue

            wheel_name = os.path.basename(req_as_url.path)
            emfs_wheel_path = 'packed_wheels' + '/' + wheel_name
            wheel_source = req_as_url.path

            wheel = WheelAsset(
                source=wheel_source,
                emfs_path=f'emfs:{emfs_wheel_path}',
                packed_path=emfs_wheel_path,
                local_path=pathlib.Path(wheel_source),
                status=AssetStatus(),
            )
            wheel.status.exists = wheel.local_path.is_file() if wheel.local_path else False
            manifest.wheels[wheel_name] = wheel

    def _collect_resources(self, manifest: AppConversionManifest) -> None:
        for resourcepath in self._resources_input:
            resourcepath = pathlib.Path(resourcepath)
            try:
                commonpath = pathlib.Path(
                    os.path.commonpath(
                        [os.path.abspath(resourcepath), os.path.abspath(self._app_folder)]
                    )
                )
            except ValueError:
                manifest.add_issue(
                    IssueSeverity.ERROR,
                    f"Resource {resourcepath} has no common path with app folder",
                    location=f"resources:{resourcepath}",
                )
                continue

            if commonpath.resolve() != pathlib.Path(self._app_folder).resolve():
                manifest.add_issue(
                    IssueSeverity.ERROR,
                    f"Resource {resourcepath} must be rooted at the app directory",
                    location=f"resources:{resourcepath}",
                )
                continue

            relpath = os.path.relpath(resourcepath, self._app_folder)
            asset = ResourceAsset(
                source=pathlib.Path(resourcepath),
                archive_path=relpath,
                status=AssetStatus(),
            )
            asset.status.exists = resourcepath.is_file()
            manifest.resources[str(resourcepath)] = asset

    def _collect_workers(self, manifest: AppConversionManifest) -> None:
        runtime = manifest.runtime

        if runtime == 'pyodide-worker':
            manifest.worker = WorkerAsset(
                worker_type=WorkerType.PYODIDE,
                status=AssetStatus(),
            )
        elif runtime == 'pyscript-worker':
            manifest.worker = WorkerAsset(
                worker_type=WorkerType.PYSCRIPT,
                status=AssetStatus(),
            )

        if manifest.build_pwa:
            manifest.service_worker = WorkerAsset(
                worker_type=WorkerType.SERVICE,
                status=AssetStatus(),
            )
