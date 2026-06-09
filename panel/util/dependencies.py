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
"""
from __future__ import annotations

import importlib.util
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


def _panel_dist_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "dist"


def check_frontend_resources(
    extension_name: str,
    component: str,
    bundled_subdir: str | None = None,
    js_files: list[str] | None = None,
) -> None:
    """
    Check that the compiled front-end assets for a Panel extension are
    present on disk.

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
    bundled_subdir : str, optional
        Subdirectory under ``panel/dist/bundled/`` where the front-end
        assets live.  Defaults to the lower-cased Bokeh model class name
        (e.g. ``"plotlyplot"`` for ``PlotlyPlot``).
    js_files : list[str], optional
        List of glob patterns for the required JS files under the
        bundled subdir.  If not provided, falls back to looking for any
        ``*.js`` / ``*.mjs`` file (less precise).
    """
    import fnmatch
    from ..io.resources import BUNDLE_DIR, use_cdn

    if use_cdn():
        return

    bundled_subdir = bundled_subdir or extension_name
    search_dirs = [
        BUNDLE_DIR / bundled_subdir,
        _panel_dist_dir() / "bundled" / bundled_subdir,
    ]

    found_all = False
    missing_patterns: list[str] = []

    for d in search_dirs:
        if not d.exists():
            continue
        if js_files:
            all_files = {
                p.relative_to(d).as_posix()
                for p in d.rglob("*")
                if p.is_file()
            }
            missing = [
                pat for pat in js_files
                if not any(fnmatch.fnmatch(f, pat) for f in all_files)
            ]
            if not missing:
                found_all = True
                break
            missing_patterns = missing
        else:
            js_files_found = list(d.glob("*.js")) + list(d.glob("*.mjs"))
            if js_files_found:
                found_all = True
                break

    if found_all:
        return

    if js_files and missing_patterns:
        file_desc = "Required JS files not found: " + ", ".join(missing_patterns)
    else:
        file_desc = f"No JS bundle files found in '{bundled_subdir}/'"

    details = (
        f"The Python package appears to be installed, but the front-end "
        f"bundle for the '{extension_name}' extension could not be found. "
        f"{file_desc}. "
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
            f"missing ({file_desc})"
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
    bundled_subdir: str | None = None,
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
        This does **not** assert that ``pn.extension(name)`` has been
        called (that is handled automatically by Panel's lazy-load
        mechanism).
    bundled_subdir : str, optional
        Subdirectory under ``panel/dist/bundled/`` where the front-end
        assets live.  Defaults to ``extension_name``.
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
        check_frontend_resources(
            extension_name, component,
            bundled_subdir=bundled_subdir,
        )


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
    min_version: str
    pip_package: str
    conda_package: str | Sequence[str]
    conda_channel: str
    extras: str
    extension_name: str
    bundled_subdir: str
    js_files: list[str]
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
        "bundled_subdir": "plotlyplot",
        "js_files": ["plotly-*.min.js"],
        "check_js_resources": True,
        "check_python": False,
    },
    "altair": {
        "component": "Vega pane (Altair support)",
        "python_package": "altair",
        "min_version": "4.0.0",
        "pip_package": "altair",
        "conda_package": "altair",
        "conda_channel": "conda-forge",
        "check_js_resources": False,
        "check_python": True,
    },
    "vega": {
        "component": "Vega pane",
        "extension_name": "vega",
        "bundled_subdir": "vegaplot",
        "js_files": ["vega@*", "vega-lite@*", "vega-embed@*"],
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
        "min_version": "1.0.0",
        "pip_package": "pyecharts",
        "conda_package": "pyecharts",
        "conda_channel": "conda-forge",
        "check_js_resources": False,
        "check_python": True,
    },
    "echarts": {
        "component": "ECharts pane",
        "extension_name": "echarts",
        "bundled_subdir": "echarts",
        "js_files": ["echarts@*/dist/echarts.min.js", "echarts-gl@*/dist/echarts-gl.min.js"],
        "check_js_resources": True,
        "check_python": False,
    },
    "holoviews": {
        "component": "HoloViews pane",
        "python_package": "holoviews",
        "min_version": "1.18.0",
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
        "bundled_subdir": "datatabulator",
        "js_files": ["tabulator-tables@*/dist/js/tabulator.min.js", "luxon/build/global/luxon.min.js"],
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

    All diagnostic parameters (minimum version, install commands, JS
    bundle subdirectory, required JS files, which checks to run by
    default) are sourced from the central
    :data:`OPTIONAL_DEPENDENCIES` registry.  Components should **not**
    duplicate these parameters at the call site.

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

    do_check_python = (
        check_python if check_python is not None
        else cfg.get("check_python", python_package is not None)
    )
    do_check_js = (
        check_js_resources if check_js_resources is not None
        else cfg.get("check_js_resources", cfg.get("extension_name") is not None)
    )

    if do_check_python and python_package is not None:
        check_python_package(
            module_name=python_package,
            component=component,
            min_version=cfg.get("min_version"),
            pip_package=cfg.get("pip_package") or python_package,
            conda_package=cfg.get("conda_package"),
            conda_channel=cfg.get("conda_channel"),
            extras=cfg.get("extras"),
        )

    extension_name = cfg.get("extension_name")
    if do_check_js and extension_name is not None:
        check_frontend_resources(
            extension_name,
            component,
            bundled_subdir=cfg.get("bundled_subdir"),
            js_files=cfg.get("js_files"),
        )


def import_component(
    name: str,
    *,
    submodule: str | None = None,
) -> t.Any:
    """
    Import a module for a registered optional dependency.

    All diagnostic parameters (minimum version, install commands) are
    sourced from the central :data:`OPTIONAL_DEPENDENCIES` registry.

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

    module_name = (
        f"{python_package}.{submodule}" if submodule else python_package
    )
    check_python_package(
        module_name=python_package,
        component=component,
        min_version=cfg.get("min_version"),
        pip_package=cfg.get("pip_package") or python_package,
        conda_package=cfg.get("conda_package"),
        conda_channel=cfg.get("conda_channel"),
        extras=cfg.get("extras"),
    )
    return importlib.import_module(module_name)
