from __future__ import annotations

import base64
import json as _json
import os
import pathlib
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

from ... import __version__, config
from ...util import base_version
from ..application import Application, build_single_handler_application
from ..document import MockSessionContext
from ..loading import LOADING_INDICATOR_CSS_CLASS
from ..resources import (
    BASE_TEMPLATE,
    CDN_DIST,
    DIST_DIR,
    INDEX_TEMPLATE,
    Resources,
    _env as _pn_env,
    bundle_resources,
    loading_css,
)
from ..state import set_curdoc, state
from .manifest import (
    AppConversionManifest,
    AssetStatus,
    IssueSeverity,
    Runtimes,
    WorkerType,
)

if t.TYPE_CHECKING:
    from collections.abc import Sequence

BOKEH_VERSION = base_version(bokeh.__version__)
PYODIDE_VERSION = 'v0.29.3'
PYSCRIPT_VERSION = '2026.2.1'
PYODIDE_URL = f'https://cdn.jsdelivr.net/pyodide/{PYODIDE_VERSION}/full/pyodide.js'
PYODIDE_PYC_URL = f'https://cdn.jsdelivr.net/pyodide/{PYODIDE_VERSION}/pyc/pyodide.js'
PYSCRIPT_CSS = f'<link rel="stylesheet" href="https://pyscript.net/releases/{PYSCRIPT_VERSION}/core.css" />'
PYSCRIPT_CSS_OVERRIDES = f'<link rel="stylesheet" href="{CDN_DIST}css/pyscript.css" />'
PYSCRIPT_JS = f'<script type="module" src="https://pyscript.net/releases/{PYSCRIPT_VERSION}/core.js" defer></script>'
PYODIDE_JS = f'<script src="{PYODIDE_URL}" defer></script>'
PYODIDE_PYC_JS = f'<script src="{PYODIDE_PYC_URL}" defer></script>'
LOCAL_PREFIX = './'

PWA_MANIFEST_TEMPLATE = _pn_env.get_template('site.webmanifest')
SERVICE_WORKER_TEMPLATE = _pn_env.get_template('serviceWorker.js')
WEB_WORKER_TEMPLATE = _pn_env.get_template('pyodide_worker.js')
WORKER_HANDLER_TEMPLATE = _pn_env.get_template('pyodide_handler.js')

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

ICON_DIR = DIST_DIR / 'images'
PWA_IMAGES: list[pathlib.Path] = [
    ICON_DIR / 'favicon.ico',
    ICON_DIR / 'icon-vector.svg',
    ICON_DIR / 'icon-32x32.png',
    ICON_DIR / 'icon-192x192.png',
    ICON_DIR / 'icon-512x512.png',
    ICON_DIR / 'apple-touch-icon.png',
    ICON_DIR / 'index_background.png',
]


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


def pack_files(filemap: dict, destination: str | os.PathLike | t.IO):
    """
    Pack files into a zipfile for distribution
    """
    with ZipFile(destination, 'w') as packfile:
        for fname, arcname in filemap.items():
            packfile.write(fname, arcname=arcname)


def loading_resources(template, inline) -> list[str]:
    css_resources = []
    if template in (BASE_TEMPLATE, FILE):
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
        pyconfig = _json.dumps({
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


class ManifestPersistence:
    def __init__(
        self,
        manifest: AppConversionManifest,
        *,
        title: str | None = None,
        panel_version: t.Literal['auto', 'local'] | str = 'auto',
        local_prefix: str = LOCAL_PREFIX,
        pwa_config: dict[t.Any, t.Any] | None = None,
    ) -> None:
        self._manifest = manifest
        self._title = title
        self._panel_version = panel_version
        self._local_prefix = local_prefix
        self._pwa_config = pwa_config or {}

    def persist_all(self) -> None:
        self.localize_requirement_urls()
        self.pack_resources_zip()
        self.generate_html_and_worker()
        self.write_outputs()
        if self._manifest.build_pwa:
            self.write_pwa_assets()

    def localize_requirement_urls(self) -> None:
        manifest = self._manifest
        localized: list[str] = []
        for req in manifest.requirements:
            try:
                req_as_url = urlparse(req)
            except ValueError:
                localized.append(req)
                continue

            if req_as_url.scheme == 'file':
                wheel_name = os.path.basename(req_as_url.path)
                if wheel_name in manifest.wheels and manifest.wheels[wheel_name].emfs_path:
                    localized.append(manifest.wheels[wheel_name].emfs_path)
                else:
                    localized.append(req)
            else:
                localized.append(req)
        manifest.requirements = localized

    def pack_resources_zip(self) -> None:
        manifest = self._manifest
        filemap: dict[str | os.PathLike, str] = {}

        for wheel in manifest.wheels.values():
            if wheel.packed_path and wheel.local_path:
                filemap[str(wheel.local_path)] = wheel.packed_path

        for resource in manifest.resources.values():
            filemap[str(resource.source)] = resource.archive_path

        if not filemap:
            manifest.resources_zip = None
            return

        zip_name = f'{manifest.app_name}.resources.zip'
        zip_path = manifest.dest_path / zip_name
        manifest.dest_path.mkdir(parents=True, exist_ok=True)

        pack_files(filemap, zip_path)
        manifest.resources_zip = zip_path

    def generate_html_and_worker(self) -> None:
        manifest = self._manifest

        pwa_manifest_ref = 'site.webmanifest' if manifest.build_pwa else None
        resources_zip_name = manifest.resources_zip.name if manifest.resources_zip else None

        html, web_worker = script_to_html(
            manifest.app_path,
            requirements=list(manifest.requirements),
            app_resources=resources_zip_name,
            runtime=manifest.runtime,
            prerender=manifest.prerender,
            panel_version=self._panel_version,
            local_prefix=self._local_prefix,
            manifest=pwa_manifest_ref,
            inline=manifest.inline,
            compiled=manifest.compiled,
        )

        manifest.html_content = html
        manifest.html_output = manifest.dest_path / f'{manifest.app_name}.html'

        if web_worker is not None and manifest.worker is not None:
            manifest.worker.content = web_worker
            if manifest.worker.worker_type == WorkerType.PYODIDE:
                manifest.worker.output_path = (
                    manifest.dest_path / f'{manifest.app_name}.js'
                )
            elif manifest.worker.worker_type == WorkerType.PYSCRIPT:
                manifest.worker.output_path = (
                    manifest.dest_path / f'{manifest.app_name}.py'
                )

    def write_outputs(self) -> None:
        manifest = self._manifest
        manifest.dest_path.mkdir(parents=True, exist_ok=True)

        if manifest.html_output and manifest.html_content:
            with open(manifest.html_output, 'w', encoding='utf-8') as out:
                out.write(manifest.html_content)

        if manifest.worker is not None and manifest.worker.content and manifest.worker.output_path:
            with open(manifest.worker.output_path, 'w', encoding='utf-8') as out:
                out.write(manifest.worker.content)
            manifest.worker.status.exists = True

    def write_pwa_assets(self) -> None:
        manifest = self._manifest
        if not manifest.build_pwa:
            return

        imgs_path = manifest.dest_path / 'images'
        imgs_path.mkdir(exist_ok=True)
        img_rel: list[str] = []
        for img in PWA_IMAGES:
            dest = imgs_path / img.name
            with open(dest, 'wb') as f:
                f.write(img.read_bytes())
            img_rel.append(f'images/{img.name}')
            manifest.pwa_icons[str(img.name)] = AssetStatus(
                exists=True, validated=True, errors=[], warnings=[],
            )

        title = self._title or 'Panel Applications'
        manifest_path = manifest.dest_path / 'site.webmanifest'

        if manifest.service_worker is not None:
            worker_content = SERVICE_WORKER_TEMPLATE.render(
                uuid=uuid.uuid4().hex,
                name=title,
                pre_cache=', '.join([repr(p) for p in img_rel]),
            )
            sw_path = manifest.dest_path / 'serviceWorker.js'
            with open(sw_path, 'w', encoding='utf-8') as f:
                f.write(worker_content)
            manifest.service_worker.content = worker_content
            manifest.service_worker.output_path = sw_path
            manifest.service_worker.status.exists = True

        files_dict = {manifest.app_name.replace('_', ' '): f'{manifest.app_name}.html'}
        pwa_manifest_content = build_pwa_manifest(files_dict, title=title, **self._pwa_config)
        with open(manifest_path, 'w', encoding='utf-8') as f:
            f.write(pwa_manifest_content)
        manifest.pwa_manifest_path = manifest_path

    def write_index(self, files: dict[str, str]) -> pathlib.Path:
        index_html = make_index(
            files, title=self._title, manifest=self._manifest.build_pwa
        )
        index_path = self._manifest.dest_path / 'index.html'
        with open(index_path, 'w') as f:
            f.write(index_html)
        return index_path
