"""
Unified runtime diagnostics layer for optional Panel dependencies.

This module provides a consistent way to check for and report on missing
or misconfigured optional dependencies across Panel's integration points
(Plotly, Altair/Vega, ECharts, HoloViews, Tabulator, etc.).

Instead of each pane/widget throwing its own ad-hoc ImportError, components
should route their optional-import paths through the helpers defined here.
This guarantees that error messages always include:

  * Which component is affected (e.g. "Plotly pane", "Tabulator widget")
  * The exact pip/conda install command
  * A clear description of the problem (missing package, version too old,
    front-end extension not built, JS resources missing even though the
    Python package is installed)

All version bounds and front-end resource paths are resolved from a single
source of truth:

  * Python package minima come from ``pyproject.toml``
    (``[project.optional-dependencies]``) plus the minimum versions that
    Panel actually tests against in its own test suite.
  * Front-end JS paths are derived directly from each Bokeh model's
    ``__javascript_raw__`` URLs using the same resolution that
    ``bundled_files()`` and the compiler use, so the check looks for the
    exact file on disk (not a loose glob).
"""
from __future__ import annotations

import functools
import importlib.util
import re
import typing as t

from pathlib import Path

from packaging.version import Version, InvalidVersion

if t.TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = (
    "DependencyIssue",
    "OptionalDependencyError",
    "OPTIONAL_DEPENDENCIES",
    "check_python_package",
    "check_frontend_resources",
    "check_extension_loaded",
    "require_optional",
    "require_component",
    "import_optional",
    "import_component",
)

_PANEL_ROOT = Path(__file__).resolve().parent.parent.parent
_PYPROJECT_TOML = _PANEL_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# Custom exception types
# ---------------------------------------------------------------------------


class DependencyIssue:
    """Enumerates the kinds of problems that can be diagnosed."""

    MISSING_PACKAGE = "missing_package"
    VERSION_TOO_OLD = "version_too_old"
    EXTENSION_NOT_LOADED = "extension_not_loaded"
    JS_RESOURCES_MISSING = "js_resources_missing"


class OptionalDependencyError(ImportError):
    """
    An ImportError subclass carrying structured diagnostics about an
    optional-dependency problem.

    Attributes
    ----------
    component : str
        Human-readable name of the Panel component that is affected
        (e.g. ``"Plotly pane"``, ``"Tabulator widget"``).
    issue : str
        One of the :class:`DependencyIssue` constants describing the
        category of problem.
    package : str | None
        The PyPI/conda package name that is missing or too old, if any.
    min_version : str | None
        The minimum required version, if a version check failed.
    installed_version : str | None
        The version that was actually found, if available.
    install_command : str
        The exact command the user should run (pip or conda).
    details : str
        Additional free-form details explaining the problem.
    """

    def __init__(
        self,
        message: str,
        component: str,
        issue: str,
        package: str | None = None,
        min_version: str | None = None,
        installed_version: str | None = None,
        install_command: str | None = None,
        details: str = "",
    ) -> None:
        super().__init__(message)
        self.component = component
        self.issue = issue
        self.package = package
        self.min_version = min_version
        self.installed_version = installed_version
        self.install_command = install_command or ""
        self.details = details


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _pip_install(packages: str | Sequence[str], extras: str | None = None) -> str:
    if isinstance(packages, str):
        packages = [packages]
    spec = " ".join(packages)
    if extras:
        spec = f"{spec}[{extras}]"
    return f"pip install {spec}"


def _conda_install(packages: str | Sequence[str], channel: str | None = None) -> str:
    if isinstance(packages, str):
        packages = [packages]
    spec = " ".join(packages)
    prefix = f"conda install -c {channel} " if channel else "conda install "
    return f"{prefix}{spec}"


def _default_install(
    pip_packages: str | Sequence[str],
    conda_packages: str | Sequence[str] | None = None,
    conda_channel: str | None = None,
    extras: str | None = None,
) -> str:
    pip_cmd = _pip_install(pip_packages, extras=extras)
    if conda_packages is None:
        return pip_cmd
    conda_cmd = _conda_install(conda_packages, channel=conda_channel)
    return f"{pip_cmd}  (or: {conda_cmd})"


def _format_message(
    component: str,
    problem: str,
    install_command: str,
    details: str = "",
) -> str:
    lines = [
        f"The {component} requires a dependency that is not available: {problem}.",
        f"To fix, run:  {install_command}",
    ]
    if details:
        lines.append(details)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Single source of truth: resolve versions & JS paths from the real codebase
# ---------------------------------------------------------------------------


@functools.cache
def _parse_pyproject_versions() -> dict[str, str]:
    """
    Return ``{pip_package_name: min_version}`` parsed from
    ``pyproject.toml``'s ``[project.dependencies]`` and
    ``[project.optional-dependencies]`` sections.

    Only the lower bound (``>=``) is captured; upper bounds and exact
    pins are ignored (Panel only cares about the minimum it was built
    against).
    """
    result: dict[str, str] = {}
    if not _PYPROJECT_TOML.is_file():
        return result

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

    if tomllib is None:
        text = _PYPROJECT_TOML.read_text(encoding="utf-8")
        dep_re = re.compile(
            r"""^\s*['"]?                          # optional leading quote
                (?P<name>[A-Za-z0-9_.\-]+)         # package name
                \s*(?P<constraint>[^'"\],#]*)      # version constraint etc.
            """,
            re.VERBOSE,
        )
        ver_re = re.compile(r'>=\s*([0-9][0-9A-Za-z.\-+]*)')
        in_deps = False
        bracket_depth = 0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line == '[project.dependencies]':
                in_deps = True
                bracket_depth = 0
                continue
            if line.startswith('[project.optional-dependencies'):
                in_deps = True
                bracket_depth = 0
                continue
            if in_deps and line.startswith('[') and 'dependencies' not in line:
                in_deps = False
                continue
            if not in_deps:
                continue
            bracket_depth += line.count('[') - line.count(']')
            if line.startswith('#') or not line:
                continue
            if line.endswith(('=', '= ')):
                continue
            m = dep_re.match(line)
            if not m:
                continue
            name = m.group('name').lower().replace('_', '-')
            constraint = m.group('constraint')
            vm = ver_re.search(constraint)
            if vm:
                result[name] = vm.group(1)
        return result

    with open(_PYPROJECT_TOML, "rb") as f:
        data = tomllib.load(f)

    ver_re = re.compile(r'>=\s*([0-9][0-9A-Za-z.\-+]*)')

    project = data.get("project", {})
    all_deps: list[str] = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        all_deps.extend(extra_deps)

    for dep in all_deps:
        dep = dep.strip()
        if not dep:
            continue
        m = re.match(r'^([A-Za-z0-9_.\-]+)', dep)
        if not m:
            continue
        name = m.group(1).lower().replace('_', '-')
        vm = ver_re.search(dep)
        if vm:
            result[name] = vm.group(1)

    return result


# Additional minimum versions that Panel actually relies on but which are
# not (yet) declared in pyproject.toml optional-dependencies.  These come
# from the test suite's own branch logic (e.g. altair 4.x vs 5.x config
# shape differences) and from the public API of the wrapped library.
_TEST_SUITE_VERSION_FLOORS: dict[str, str] = {
    "altair": "4.0.0",
    "pyecharts": "1.0.0",
}


@functools.cache
def _resolve_python_min_version(pip_package: str) -> str | None:
    """
    Return the minimum acceptable version for ``pip_package``, combining
    ``pyproject.toml`` declarations with the internal test-suite floors.

    Returns ``None`` if no minimum is known for the package.
    """
    key = pip_package.lower().replace('_', '-')
    pyproject_ver = _parse_pyproject_versions().get(key)
    test_ver = _TEST_SUITE_VERSION_FLOORS.get(key)
    if pyproject_ver is None:
        return test_ver
    if test_ver is None:
        return pyproject_ver
    return pyproject_ver if Version(pyproject_ver) >= Version(test_ver) else test_ver


@functools.cache
def _resolve_js_asset_paths(extension_name: str) -> tuple[str, list[Path]]:
    """
    Derive the exact on-disk paths of every JS asset required by the
    Bokeh model registered under ``extension_name``.

    The resolution logic mirrors :func:`panel.io.resources.bundled_files`
    and :func:`panel.compiler.write_bundled_files`, so the returned
    ``Path`` objects point at exactly the files that a successful build
    would have produced.

    Returns
    -------
    bundled_subdir : str
        The subdirectory under ``panel/dist/bundled/`` that contains the
        assets (matches the Bokeh model class name, lower-cased).
    expected_files : list[Path]
        Absolute paths to every JS file that must exist for the extension
        to work in non-CDN mode.  These are concrete, versioned filenames
        (e.g. ``.../plotlyplot/plotly-3.1.0.min.js``), never globs.
    """
    from ..config import config, panel_extension
    from ..io.resources import BUNDLE_DIR

    module_path = panel_extension._imports.get(extension_name)
    if module_path is None:
        raise ValueError(f"No panel extension registered under name '{extension_name}'")

    module = importlib.import_module(module_path)

    model_cls = None
    for attr in dir(module):
        obj = getattr(module, attr, None)
        try:
            from bokeh.model import Model
        except Exception:
            Model = None  # type: ignore[assignment,misc]
        if Model is not None and isinstance(obj, type) and issubclass(obj, Model):
            js_raw = getattr(obj, '__javascript_raw__', None)
            if js_raw:
                model_cls = obj
                break

    if model_cls is None:
        raise ValueError(
            f"Could not find a Bokeh Model with __javascript_raw__ in "
            f"module '{module_path}' (extension '{extension_name}')"
        )

    bundled_subdir = model_cls.__name__.lower()
    for cls in model_cls.__mro__[1:]:
        cls_files = getattr(cls, '__javascript_raw__', None)
        if cls_files is model_cls.__javascript_raw__:
            bundled_subdir = cls.__name__.lower()
            break

    raw_files = list(model_cls.__javascript_raw__)
    npm_cdn_prefixes = (
        config.npm_cdn,
        'https://cdn.jsdelivr.net/npm',
        'https://unpkg.com',
    )

    expected: list[Path] = []
    for url in raw_files:
        url = url.split('?')[0]

        if url.startswith('https://cdn.plot.ly/'):
            rel_path = url.replace('https://cdn.plot.ly/', '')
        else:
            matched_prefix = False
            for prefix in npm_cdn_prefixes:
                if url.startswith(prefix):
                    rel_path = url[len(prefix):].lstrip('/')
                    matched_prefix = True
                    break
            if not matched_prefix:
                parts = url.split('//', 1)[-1].split('/', 1)
                rel_path = parts[1] if len(parts) > 1 else url

        expected.append(BUNDLE_DIR / bundled_subdir / rel_path)

    return bundled_subdir, expected


# ---------------------------------------------------------------------------
# Python package checks
# ---------------------------------------------------------------------------


def check_python_package(
    module_name: str,
    component: str,
    min_version: str | None = None,
    pip_package: str | None = None,
    conda_package: str | Sequence[str] | None = None,
    conda_channel: str | None = None,
    extras: str | None = None,
    import_error_message: str = "",
) -> None:
    """
    Verify that a Python package is installed and (optionally) meets a
    minimum version requirement.

    Raises
    ------
    OptionalDependencyError
        If the package is missing or too old, with a message that
        includes the affected component and the exact install command.
    """
    pip_package = pip_package or module_name
    install_command = _default_install(
        pip_package, conda_packages=conda_package,
        conda_channel=conda_channel, extras=extras,
    )

    spec = importlib.util.find_spec(module_name)
    if spec is None:
        msg = _format_message(
            component=component,
            problem=f"Python package '{pip_package}' is not installed",
            install_command=install_command,
            details=import_error_message,
        )
        raise OptionalDependencyError(
            msg,
            component=component,
            issue=DependencyIssue.MISSING_PACKAGE,
            package=pip_package,
            install_command=install_command,
            details=import_error_message,
        )

    if min_version is None:
        return

    module = importlib.import_module(module_name)
    raw_version = getattr(module, "__version__", None)
    if raw_version is None:
        for attr in ("version", "VERSION"):
            raw_version = getattr(module, attr, None)
            if raw_version is not None:
                break
    if raw_version is None:
        return

    try:
        installed = Version(str(raw_version))
        required = Version(min_version)
    except InvalidVersion:
        return

    if installed < required:
        problem = (
            f"Python package '{pip_package}' version {raw_version} is "
            f"too old; >= {min_version} is required"
        )
        msg = _format_message(
            component=component,
            problem=problem,
            install_command=install_command,
        )
        raise OptionalDependencyError(
            msg,
            component=component,
            issue=DependencyIssue.VERSION_TOO_OLD,
            package=pip_package,
            min_version=min_version,
            installed_version=str(raw_version),
            install_command=install_command,
        )


# ---------------------------------------------------------------------------
# Front-end / extension checks
# ---------------------------------------------------------------------------


def check_frontend_resources(
    extension_name: str,
    component: str,
) -> None:
    """
    Check that the compiled front-end assets for a Panel extension are
    present on disk.

    Asset paths are not guessed from glob strings.  They are derived
    directly from the registered Bokeh model's ``__javascript_raw__``
    URLs using the same URL→path translation as
    :func:`panel.io.resources.bundled_files` and the compiler, so this
    function checks for the exact versioned files that ``panel build``
    would have produced (e.g. ``plotlyplot/plotly-3.1.0.min.js``).

    This catches the case where the Python package is installed but the
    JS/CSS bundles have not been built (e.g. an editable install from
    source without running the build step).

    Parameters
    ----------
    extension_name : str
        The Panel extension name (matches keys in
        ``panel.config.panel_extension._imports``).
    component : str
        Human-readable component name for error messages.
    """
    from ..io.resources import use_cdn

    if use_cdn():
        return

    bundled_subdir, expected_files = _resolve_js_asset_paths(extension_name)

    missing: list[Path] = [p for p in expected_files if not p.is_file()]
    if not missing:
        return

    from ..io.resources import BUNDLE_DIR
    rel_missing = [
        str(p.relative_to(BUNDLE_DIR)) if p.is_relative_to(BUNDLE_DIR)
        else str(p)
        for p in missing
    ]
    file_desc = (
        "Required JS files not found:\n  " + "\n  ".join(rel_missing)
    )

    details = (
        f"The Python package appears to be installed, but the front-end "
        f"bundle for the '{extension_name}' extension could not be found. "
        f"{file_desc}\n\n"
        "This usually happens when using an editable/development install "
        "without first building the JavaScript resources."
        "\n\n"
        "To build the front-end bundles, run from the panel source root:\n"
        "  pip install build\n"
        "  python -m build --wheel\n"
        "or, for a live dev build:\n"
        "  panel build"
    )
    msg = _format_message(
        component=component,
        problem=(
            f"front-end JS resources for extension '{extension_name}' are "
            f"missing"
        ),
        install_command="pip install build && python -m build --wheel",
        details=details,
    )
    raise OptionalDependencyError(
        msg,
        component=component,
        issue=DependencyIssue.JS_RESOURCES_MISSING,
        package="panel",
        install_command="pip install build && python -m build --wheel",
        details=details,
    )


def check_extension_loaded(
    extension_name: str,
    component: str,
) -> None:
    """
    Verify that ``pn.extension(...)`` has been invoked with the given
    extension name, so the required JS/CSS resources will be loaded.
    """
    from ..config import panel_extension
    from ..io.state import state

    loaded = panel_extension._loaded_extensions
    if extension_name in loaded:
        return

    doc_exts = state._extensions.get(state.curdoc, []) if state.curdoc else []
    if extension_name in doc_exts:
        return

    problem = (
        f"the Panel extension '{extension_name}' has not been loaded. "
        f"Call `pn.extension('{extension_name}')` at the top of your "
        "application or notebook before rendering this component."
    )
    install_cmd = f"pn.extension('{extension_name}')"
    msg = _format_message(
        component=component,
        problem=problem,
        install_command=install_cmd,
        details=(
            "Panel extensions must be declared explicitly so that the "
            "required JavaScript and CSS resources are included in the "
            "rendered page."
        ),
    )
    raise OptionalDependencyError(
        msg,
        component=component,
        issue=DependencyIssue.EXTENSION_NOT_LOADED,
        install_command=install_cmd,
    )


# ---------------------------------------------------------------------------
# High-level public helpers
# ---------------------------------------------------------------------------


def require_optional(
    component: str,
    *,
    python_package: str | None = None,
    min_version: str | None = None,
    pip_package: str | None = None,
    conda_package: str | Sequence[str] | None = None,
    conda_channel: str | None = None,
    extras: str | None = None,
    extension_name: str | None = None,
    check_js_resources: bool = True,
) -> None:
    """
    One-stop diagnostic that chains the relevant checks for an optional
    dependency.

    Parameters
    ----------
    component : str
        Human-readable component name (shown in error messages).
    python_package : str, optional
        The importable module name to check for (e.g. ``"plotly"``).
    min_version : str, optional
        Minimum acceptable version for ``python_package``.
    pip_package, conda_package, conda_channel, extras
        Used to construct the install command shown to the user.
    extension_name : str, optional
        If supplied with ``check_js_resources=True``, verify that the
        compiled front-end bundle for this extension exists on disk.
        Asset paths are resolved from the extension's Bokeh model
        directly (no globs).  This does **not** assert that
        ``pn.extension(name)`` has been called (that is handled
        automatically by Panel's lazy-load mechanism).
    check_js_resources : bool, default True
        Whether to verify that the front-end bundle files exist on disk
        (only meaningful when using local/server resources, not CDN).
    """
    if python_package is not None:
        check_python_package(
            module_name=python_package,
            component=component,
            min_version=min_version,
            pip_package=pip_package,
            conda_package=conda_package,
            conda_channel=conda_channel,
            extras=extras,
        )

    if extension_name is not None and check_js_resources:
        check_frontend_resources(extension_name, component)


def import_optional(
    module_name: str,
    component: str,
    *,
    min_version: str | None = None,
    pip_package: str | None = None,
    conda_package: str | Sequence[str] | None = None,
    conda_channel: str | None = None,
    extras: str | None = None,
) -> t.Any:
    """
    Import a module, raising :class:`OptionalDependencyError` (with a
    nice install command) if it is missing or too old.

    Returns the imported module on success.
    """
    check_python_package(
        module_name=module_name,
        component=component,
        min_version=min_version,
        pip_package=pip_package,
        conda_package=conda_package,
        conda_channel=conda_channel,
        extras=extras,
    )
    return importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# Central registry of known optional dependencies
# ---------------------------------------------------------------------------


class _ComponentConfig(t.TypedDict, total=False):
    component: str
    python_package: str
    pip_package: str
    conda_package: str | Sequence[str]
    conda_channel: str
    extras: str
    extension_name: str
    check_js_resources: bool
    check_python: bool


OPTIONAL_DEPENDENCIES: t.Final[dict[str, _ComponentConfig]] = {
    "plotly": {
        "component": "Plotly pane",
        "python_package": "plotly",
        "pip_package": "plotly",
        "conda_package": "plotly",
        "conda_channel": "plotly",
        "extras": "recommended",
        "extension_name": "plotly",
        "check_js_resources": True,
        "check_python": False,
    },
    "altair": {
        "component": "Vega pane (Altair support)",
        "python_package": "altair",
        "pip_package": "altair",
        "conda_package": "altair",
        "conda_channel": "conda-forge",
        "check_js_resources": False,
        "check_python": True,
    },
    "vega": {
        "component": "Vega pane",
        "extension_name": "vega",
        "check_js_resources": True,
        "check_python": False,
    },
    "vl_convert": {
        "component": "Vega pane export",
        "python_package": "vl_convert",
        "pip_package": "vl-convert-python",
        "conda_package": "vl-convert-python",
        "conda_channel": "conda-forge",
        "check_js_resources": False,
        "check_python": True,
    },
    "pyecharts": {
        "component": "ECharts pane (pyecharts support)",
        "python_package": "pyecharts",
        "pip_package": "pyecharts",
        "conda_package": "pyecharts",
        "conda_channel": "conda-forge",
        "check_js_resources": False,
        "check_python": True,
    },
    "echarts": {
        "component": "ECharts pane",
        "extension_name": "echarts",
        "check_js_resources": True,
        "check_python": False,
    },
    "holoviews": {
        "component": "HoloViews pane",
        "python_package": "holoviews",
        "pip_package": "holoviews",
        "conda_package": "holoviews",
        "conda_channel": "conda-forge",
        "extras": "recommended",
        "check_js_resources": False,
        "check_python": True,
    },
    "tabulator": {
        "component": "Tabulator widget",
        "extension_name": "tabulator",
        "check_js_resources": True,
        "check_python": False,
    },
}


# ---------------------------------------------------------------------------
# Registry-backed helpers
# ---------------------------------------------------------------------------


def _get_config(name: str) -> _ComponentConfig:
    if name not in OPTIONAL_DEPENDENCIES:
        known = ", ".join(sorted(OPTIONAL_DEPENDENCIES))
        raise ValueError(
            f"Unknown optional dependency '{name}'. "
            f"Known components: {known}"
        )
    return OPTIONAL_DEPENDENCIES[name]


def require_component(
    name: str,
    *,
    check_js_resources: bool | None = None,
    check_python: bool | None = None,
) -> None:
    """
    Run diagnostics for a registered optional dependency.

    All diagnostic parameters are resolved from single sources of truth:

    * **Python minimum version** — parsed from ``pyproject.toml``
      (``[project.dependencies]`` and ``[project.optional-dependencies]``)
      plus the minimum versions Panel's own test suite branches on
      (see :func:`_resolve_python_min_version`).
    * **Front-end JS asset paths** — derived directly from the Bokeh
      model registered under the extension name, using the same URL
      resolution as :func:`panel.io.resources.bundled_files` and the
      compiler.  No glob strings are used.

    Components should **not** duplicate these parameters at the call
    site; the registry plus the real codebase is the single source.

    Parameters
    ----------
    name : str
        Key into :data:`OPTIONAL_DEPENDENCIES` (e.g. ``"plotly"``,
        ``"holoviews"``, ``"tabulator"``).
    check_js_resources : bool, optional
        Override whether to verify front-end JS bundles exist on disk
        (skipped automatically when CDN mode is active).  Defaults to
        the value declared in the registry.
    check_python : bool, optional
        Override whether to verify the Python package is installed and
        meets the minimum version requirement.  Defaults to the value
        declared in the registry.

    Raises
    ------
    OptionalDependencyError
        With component name, missing object, install command, and the
        affected entry point in the message.
    ValueError
        If ``name`` is not a registered component.
    """
    cfg = _get_config(name)
    component = cfg.get("component", name)
    python_package = cfg.get("python_package")
    pip_package = cfg.get("pip_package") or python_package

    do_check_python = (
        check_python if check_python is not None
        else cfg.get("check_python", python_package is not None)
    )
    do_check_js = (
        check_js_resources if check_js_resources is not None
        else cfg.get("check_js_resources", cfg.get("extension_name") is not None)
    )

    if do_check_python and python_package is not None:
        min_version = _resolve_python_min_version(pip_package or python_package)
        check_python_package(
            module_name=python_package,
            component=component,
            min_version=min_version,
            pip_package=pip_package,
            conda_package=cfg.get("conda_package"),
            conda_channel=cfg.get("conda_channel"),
            extras=cfg.get("extras"),
        )

    extension_name = cfg.get("extension_name")
    if do_check_js and extension_name is not None:
        check_frontend_resources(extension_name, component)


def import_component(
    name: str,
    *,
    submodule: str | None = None,
) -> t.Any:
    """
    Import a module for a registered optional dependency.

    The minimum version requirement is resolved from ``pyproject.toml``
    and Panel's internal test-suite floors — see
    :func:`_resolve_python_min_version`.  No hand-written
    ``min_version`` field is needed in the registry.

    Parameters
    ----------
    name : str
        Key into :data:`OPTIONAL_DEPENDENCIES`.
    submodule : str, optional
        Dotted sub-path appended to the base ``python_package``, e.g.
        ``"graph_objs"`` on top of ``"plotly"`` gives
        ``plotly.graph_objs``.

    Returns
    -------
    module
        The imported module on success.

    Raises
    ------
    OptionalDependencyError
        If the package is missing or too old, with full diagnostic info
        (component name, install command, version info).
    ValueError
        If ``name`` is not registered or has no ``python_package``.
    """
    cfg = _get_config(name)
    component = cfg.get("component", name)
    python_package = cfg.get("python_package")
    if python_package is None:
        raise ValueError(
            f"Component '{name}' does not declare a python_package; "
            "it has no Python module to import."
        )

    pip_package = cfg.get("pip_package") or python_package
    module_name = (
        f"{python_package}.{submodule}" if submodule else python_package
    )
    min_version = _resolve_python_min_version(pip_package)
    check_python_package(
        module_name=python_package,
        component=component,
        min_version=min_version,
        pip_package=pip_package,
        conda_package=cfg.get("conda_package"),
        conda_channel=cfg.get("conda_channel"),
        extras=cfg.get("extras"),
    )
    return importlib.import_module(module_name)
