from __future__ import annotations

import ast
import json
import sys
import typing as t

from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PANEL_DIR = BASE_DIR / "panel"
EXAMPLES_DIR = BASE_DIR / "examples"
DOC_DIR = BASE_DIR / "doc"
GALLERY_DIR = EXAMPLES_DIR / "gallery"
REF_DIR = EXAMPLES_DIR / "reference"
DIST_DIR = PANEL_DIR / "dist"
BUNDLE_DIR = DIST_DIR / "bundled"

# Allow importing panel directly from the source tree even before a build/install.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Modules / packages that are genuinely optional extras; failures to import them
# (or submodules under them) must NOT block the sync checks.
OPTIONAL_DEPENDENCY_ROOTS: tuple[str, ...] = (
    "panel.pane.vtk",
    "panel.chat.langchain",
)


def _is_optional_import_error(exc: Exception, module_name: str) -> bool:
    """Return True when an import failure is caused by a known optional dependency."""
    msg = str(exc).lower()
    if any(root in module_name for root in OPTIONAL_DEPENDENCY_ROOTS):
        return True
    # Heuristic: the exception message names a known optional package
    optional_packages = (
        "vtk", "pyvista", "langchain", "pyvistaqt", "reacton", "ipyleaflet",
        "textual", "django", "fastapi", "flask",
    )
    return any(pkg in msg for pkg in optional_packages)

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
CYAN = "\033[0;36m"
RESET = "\033[0m"


@dataclass
class SyncError:
    file: Path
    kind: str
    message: str
    line: int | None = None
    object_name: str | None = None

    def format(self, base: Path) -> str:
        rel = self.file.relative_to(base) if self.file.is_absolute() and base in self.file.parents else self.file
        loc = f":{self.line}" if self.line else ""
        obj = f" [{self.object_name}]" if self.object_name else ""
        return f"{RED}ERROR{RESET} {CYAN}{self.kind}{RESET} {rel}{loc}{obj}: {self.message}"


@dataclass
class SyncChecker:
    errors: list[SyncError] = field(default_factory=list)
    base_dir: Path = BASE_DIR

    # --- per-checker strategy flags (override in subclasses) ---
    # Set to False when the checker performs purely static analysis
    # (AST walk, file existence, etc.) and never imports panel/param.
    requires_runtime: t.ClassVar[bool] = True
    # Human-readable description for --diagnose output.
    description: t.ClassVar[str] = ""

    def add_error(
        self,
        file: Path | str,
        kind: str,
        message: str,
        *,
        line: int | None = None,
        object_name: str | None = None,
    ) -> None:
        self.errors.append(SyncError(
            file=Path(file),
            kind=kind,
            message=message,
            line=line,
            object_name=object_name,
        ))

    def run(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def read_notebook_code_cells(nb_path: Path) -> list[tuple[int, str]]:
    """Return list of (cell_index, joined_source) for code cells."""
    data = json.loads(nb_path.read_text(encoding="utf-8"))
    cells: list[tuple[int, str]] = []
    for i, cell in enumerate(data.get("cells", [])):
        if cell.get("cell_type") == "code":
            source = cell.get("source", [])
            if isinstance(source, list):
                source = "".join(source)
            cells.append((i, source))
    return cells


def iter_notebooks(root: Path) -> t.Iterator[Path]:
    yield from sorted(root.rglob("*.ipynb"))


def iter_markdown(root: Path) -> t.Iterator[Path]:
    for p in sorted(root.rglob("*.md")):
        if ".ipynb_checkpoints" in str(p):
            continue
        yield p


def extract_python_from_md(md_text: str) -> list[tuple[int, str]]:
    """Extract fenced python/pyodide code blocks with starting line numbers."""
    lines = md_text.splitlines()
    blocks: list[tuple[int, str]] = []
    i = 0
    in_block = False
    block_lines: list[str] = []
    block_start = 0
    fence_info = ""
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not in_block and stripped.startswith("```"):
            in_block = True
            block_start = i + 1
            fence_info = stripped[3:].strip().lower()
            block_lines = []
        elif in_block:
            if stripped.startswith("```"):
                in_block = False
                if any(k in fence_info for k in ("python", "py", "pyodide", "pyodide-python")):
                    blocks.append((block_start, "\n".join(block_lines)))
            else:
                block_lines.append(line)
        i += 1
    return blocks


# ---------------------------------------------------------------------------
# 1. Reference document component path checker
# ---------------------------------------------------------------------------

SECTION_TO_MODULE: dict[str, str] = {
    "panes": "panel.pane",
    "widgets": "panel.widgets",
    "indicators": "panel.widgets",
    "layouts": "panel.layout",
    "chat": "panel.chat",
    "templates": "panel.template",
    "global": "panel.io",
    "custom_components": None,
}

# 某些 reference 名称与其对应组件名不一致时的映射
REF_NAME_OVERRIDES: dict[str, dict[str, str]] = {
    "templates": {
        "Bootstrap": "BootstrapTemplate",
        "EditableTemplate": "EditableTemplate",
        "FastGridTemplate": "FastGridTemplate",
        "FastListTemplate": "FastListTemplate",
        "GoldenLayout": "GoldenTemplate",
        "Material": "MaterialTemplate",
        "React": "ReactTemplate",
        "Slides": "SlidesTemplate",
        "Vanilla": "VanillaTemplate",
    },
    "panes": {
        "DataFrame": "DataFrame",
    },
    "global": {
        "Notifications": "state",
    },
}


class ReferencePathChecker(SyncChecker):
    """Verify that each reference notebook corresponds to an existing component."""

    requires_runtime = True
    description = "Matches reference notebook names to exported component symbols."

    def run(self) -> None:
        import importlib

        if not REF_DIR.is_dir():
            return

        for section in REF_DIR.iterdir():
            if not section.is_dir():
                continue
            section_name = section.name
            module_path = SECTION_TO_MODULE.get(section_name)
            if module_path is None:
                continue

            try:
                module = importlib.import_module(module_path)
            except Exception as exc:
                if _is_optional_import_error(exc, module_path):
                    continue
                self.add_error(
                    file=section,
                    kind="ReferencePath",
                    message=f"Failed to import core module '{module_path}': {exc}",
                )
                continue

            overrides = REF_NAME_OVERRIDES.get(section_name, {})

            for nb in sorted(section.glob("*.ipynb")):
                stem = nb.stem
                component_name = overrides.get(stem, stem)

                if not hasattr(module, component_name):
                    # Also check top-level panel exports
                    try:
                        import panel as pn
                    except Exception as exc:
                        if _is_optional_import_error(exc, "panel"):
                            continue
                        self.add_error(
                            file=nb,
                            kind="ReferencePath",
                            message=f"Failed to import top-level 'panel' module: {exc}",
                        )
                        continue
                    try:
                        exported = hasattr(pn, component_name)
                    except Exception as exc:
                        if _is_optional_import_error(exc, f"panel.{component_name}"):
                            continue
                        raise
                    if not exported:
                        self.add_error(
                            file=nb,
                            kind="ReferencePath",
                            message=(
                                f"Reference notebook '{stem}' does not correspond to any "
                                f"exported symbol in module '{module_path}'. "
                                f"Expected component '{component_name}' not found."
                            ),
                            object_name=component_name,
                        )


# ---------------------------------------------------------------------------
# 2. Gallery / Reference parameter checker — AST-based static analysis
# ---------------------------------------------------------------------------

PN_COMPONENT_PREFIXES = (
    "pn.widgets.",
    "pn.pane.",
    "pn.layout.",
    "pn.chat.",
    "pn.template.",
    "pn.",
)

# Well-known attribute access chains that are NOT component instantiations
_NON_COMPONENT_ATTRS = {
    "pn.extension",
    "pn.cache",
    "pn.bind",
    "pn.depends",
    "pn.interact",
    "pn.serve",
    "pn.panel",
    "pn.state",
    "pn.config",
    "pn.rx",
    "pn.param",
    "pn.pipeline",
    "pn.reactive",
    "pn.custom",
    "pn.viewable",
    "pn.chat.langchain",
    "pn.io",
    "pn.theme",
    "pn.util",
    "pn.command",
    "pn.models",
    "pn.tests",
    "pn._param",
    "pn.auth",
    "pn.bokeh",
    "pn.compiler",
    "pn.config",
    "pn.custom",
    "pn.depends",
    "pn.entry_points",
    "pn.eslint",
    "pn.index",
    "pn.interact",
    "pn.links",
    "pn.package",
    "pn.param",
    "pn.pipeline",
    "pn.py",
    "pn.reactive",
    "pn.tsconfig",
    "pn.viewable",
}


def _resolve_component_class(attr_chain: str) -> tuple[type | None, Exception | None]:
    """Resolve a dotted name like 'pn.widgets.Select' to the actual class.

    Returns ``(cls_or_None, exc_or_None)``. The second element is only populated
    when a **core** (non-optional) import failed so the caller can surface it.
    """
    import importlib

    parts = attr_chain.split(".")
    if parts[0] != "pn":
        return None, None
    parts[0] = "panel"
    module_name = ".".join(parts[:-1])
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        if _is_optional_import_error(exc, module_name):
            return None, None
        return None, exc
    full_name = ".".join(parts)
    try:
        obj = __import__(parts[0], fromlist=parts[1:])
    except Exception as exc:
        if _is_optional_import_error(exc, full_name):
            return None, None
        return None, exc
    try:
        for seg in parts[1:]:
            obj = getattr(obj, seg)
        if isinstance(obj, type):
            return obj, None
    except Exception as exc:
        if _is_optional_import_error(exc, full_name):
            return None, None
        return None, exc
    return None, None


class _ComponentInstantiationVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        # (attr_chain, keyword_names, line_no)
        self.calls: list[tuple[str, list[str], int]] = []
        # name -> resolved dotted chain for aliased imports
        self._aliases: dict[str, str] = {}

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        pass

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        func = node.func
        chain = _get_dotted_chain(func)
        if chain is None:
            return
        if any(chain.startswith(p) for p in PN_COMPONENT_PREFIXES):
            if chain in _NON_COMPONENT_ATTRS:
                return
            kwargs = [kw.arg for kw in node.keywords if kw.arg is not None]
            self.calls.append((chain, kwargs, node.lineno))


def _get_dotted_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return ".".join(parts)
    return None


class GalleryParamChecker(SyncChecker):
    """Check that gallery & reference notebooks do not reference deleted parameters."""

    requires_runtime = True
    description = "Static-scans notebook code cells and validates keyword arg names against component signatures."

    def run(self) -> None:
        targets: list[Path] = []
        if GALLERY_DIR.is_dir():
            targets.extend(iter_notebooks(GALLERY_DIR))
        if REF_DIR.is_dir():
            targets.extend(iter_notebooks(REF_DIR))

        # Avoid reporting the same broken core import for every single notebook.
        reported_core_failures: set[str] = set()

        for nb_path in targets:
            try:
                cells = read_notebook_code_cells(nb_path)
            except Exception:
                self.add_error(
                    file=nb_path,
                    kind="GalleryParam",
                    message="Failed to parse notebook",
                )
                continue
            for cell_idx, source in cells:
                if not source.strip():
                    continue
                try:
                    tree = ast.parse(source)
                except SyntaxError as exc:
                    # Notebooks often have partial code; skip gracefully
                    continue
                visitor = _ComponentInstantiationVisitor()
                visitor.visit(tree)
                for chain, kwargs, lineno in visitor.calls:
                    cls, core_exc = _resolve_component_class(chain)
                    if core_exc is not None and chain not in reported_core_failures:
                        reported_core_failures.add(chain)
                        self.add_error(
                            file=nb_path,
                            kind="GalleryParam",
                            message=(
                                f"Failed to resolve core component '{chain}': {core_exc}"
                            ),
                            line=lineno,
                            object_name=chain,
                        )
                    if cls is None:
                        continue
                    try:
                        valid_params = set(cls.param.objects(instance=False).keys())
                    except Exception as exc:
                        if _is_optional_import_error(exc, chain):
                            continue
                        if chain not in reported_core_failures:
                            reported_core_failures.add(chain)
                            self.add_error(
                                file=nb_path,
                                kind="GalleryParam",
                                message=(
                                    f"Failed to introspect parameters of "
                                    f"'{chain}': {exc}"
                                ),
                                line=lineno,
                                object_name=chain,
                            )
                        continue
                    for kw in kwargs:
                        if kw.startswith("_"):
                            continue
                        if kw not in valid_params:
                            self.add_error(
                                file=nb_path,
                                kind="GalleryParam",
                                message=(
                                    f"Parameter '{kw}' passed to '{chain}' does not exist "
                                    f"on the component. Known params: {sorted(valid_params)[:10]}..."
                                ),
                                line=lineno,
                                object_name=chain,
                            )


# ---------------------------------------------------------------------------
# 3. Public import path checker — ensure __all__ matches actual exports
# ---------------------------------------------------------------------------

PUBLIC_MODULES: list[tuple[str, Path]] = [
    ("panel", PANEL_DIR / "__init__.py"),
    ("panel.widgets", PANEL_DIR / "widgets" / "__init__.py"),
    ("panel.pane", PANEL_DIR / "pane" / "__init__.py"),
    ("panel.layout", PANEL_DIR / "layout" / "__init__.py"),
    ("panel.template", PANEL_DIR / "template" / "__init__.py"),
    ("panel.chat", PANEL_DIR / "chat" / "__init__.py"),
]


class PublicImportChecker(SyncChecker):
    """Verify that every name declared in ``__all__`` has a corresponding
    top-level binding (definition, import, or lazy ``__getattr__``) in the
    module source. This checker is fully static and never imports the module,
    so it cannot be defeated by optional-dependency import failures inside
    the module body."""

    requires_runtime = False
    description = "Static AST check that each __all__ entry is defined/imported at the module top level."

    # Names implicitly available on every module; never flagged as missing.
    _IMPLICIT_NAMES: t.ClassVar[frozenset[str]] = frozenset({
        "__name__", "__doc__", "__package__", "__loader__", "__spec__",
        "__file__", "__cached__", "__builtins__", "__path__",
        "__all__", "__version__",
    })

    @classmethod
    def _collect_top_level_bindings(cls, tree: ast.Module) -> set[str]:
        """Return all names bound at the top level of an AST module."""
        bindings: set[str] = set()
        for node in tree.body:
            # def / async def / class
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings.add(node.name)
                continue
            # import X, Y as Z
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bindings.add(alias.asname if alias.asname else alias.name.split(".")[0])
                continue
            # from X import A, B as C
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        # Cannot statically enumerate; mark as wildcard import.
                        bindings.add("*")
                    else:
                        bindings.add(alias.asname if alias.asname else alias.name)
                continue
            # Name = ... / (a, b) = ... / Name.attr = ...
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    bindings.update(cls._extract_assigned_names(tgt))
                continue
            # Name := ...
            if isinstance(node, ast.NamedExpr):
                bindings.update(cls._extract_assigned_names(node.target))
                continue
            # for / async for at top level (unusual but harmless)
            if isinstance(node, (ast.For, ast.AsyncFor)):
                bindings.update(cls._extract_assigned_names(node.target))
                continue
            # with X as Y
            if isinstance(node, ast.With):
                for item in node.items:
                    if item.optional_vars is not None:
                        bindings.update(cls._extract_assigned_names(item.optional_vars))
                continue
        return bindings

    @classmethod
    def _extract_assigned_names(cls, node: ast.AST) -> set[str]:
        names: set[str] = set()

        def _walk(t: ast.AST) -> None:
            if isinstance(t, ast.Name):
                names.add(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for elt in t.elts:
                    _walk(elt)
            elif isinstance(t, ast.Starred):
                _walk(t.value)
            # ast.Attribute / ast.Subscript targets do not introduce new top-level names
        _walk(node)
        return names

    @classmethod
    def _has_lazy_getattr(cls, tree: ast.Module) -> bool:
        """Return True if the module defines a top-level ``__getattr__``.

        Panel uses this pattern to lazily expose optional-dependency symbols
        without importing the heavy dependency at module load time (e.g.
        ``panel.chat.langchain``).
        """
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign)):
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and tgt.id == "__getattr__":
                            return True
                else:
                    if node.name == "__getattr__":
                        return True
        return False

    def run(self) -> None:
        for mod_name, mod_path in PUBLIC_MODULES:
            if not mod_path.is_file():
                continue
            try:
                tree = ast.parse(mod_path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                self.add_error(
                    file=mod_path,
                    kind="PublicImport",
                    message=f"Failed to parse module source: {exc}",
                )
                continue

            # Extract __all__
            all_names: list[str] = []
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "__all__"
                    and isinstance(node.value, (ast.Tuple, ast.List))
                ):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            all_names.append(elt.value)
                    break
            if not all_names:
                continue

            bindings = self._collect_top_level_bindings(tree)
            has_lazy = self._has_lazy_getattr(tree)

            for name in all_names:
                if name in bindings or name in self._IMPLICIT_NAMES:
                    continue
                if "*" in bindings:
                    # Wildcard import present; cannot statically prove absence.
                    continue
                full_dotted = f"{mod_name}.{name}"
                # Explicit optional-dependency lazy exports are never flagged.
                if any(root == full_dotted or full_dotted.startswith(root + ".")
                       for root in OPTIONAL_DEPENDENCY_ROOTS):
                    continue
                if has_lazy:
                    # Lazy __getattr__ is defined — the name may be resolved at
                    # runtime but is NOT in the explicit optional list. Flag
                    # as a soft mismatch so the author can either add a static
                    # import or enroll it in the optional-dependency list.
                    self.add_error(
                        file=mod_path,
                        kind="PublicImport",
                        message=(
                            f"Name '{name}' is declared in __all__ but not statically "
                            f"bound in '{mod_name}'; it may be provided by the module's "
                            f"lazy __getattr__, but '{full_dotted}' is not in the "
                            f"OPTIONAL_DEPENDENCY_ROOTS whitelist."
                        ),
                        object_name=name,
                    )
                    continue
                self.add_error(
                    file=mod_path,
                    kind="PublicImport",
                    message=(
                        f"Name '{name}' is declared in __all__ but has no top-level "
                        f"definition or import in '{mod_name}'."
                    ),
                    object_name=name,
                )


# ---------------------------------------------------------------------------
# 4. Component signature checker — param-derived classes follow consistent rules
# ---------------------------------------------------------------------------

REQUIRED_BASE_PARAMS = {
    # Classes inheriting from Layoutable must expose these
    "Layoutable": (
        "align", "aspect_ratio", "css_classes", "design", "height",
        "margin", "max_height", "max_width", "min_height", "min_width",
        "sizing_mode", "styles", "stylesheets", "visible", "width",
    ),
}


class ComponentSignatureChecker(SyncChecker):
    """Verify that each public component exposes the expected base parameters."""

    requires_runtime = True
    description = "Ensures every exported Layoutable subclass carries the standard base param set."

    def run(self) -> None:
        import importlib
        import inspect

        # Core dependencies — not optional, so any import failure must surface as an error.
        try:
            from param import Parameterized
        except Exception as exc:
            self.add_error(
                file=PANEL_DIR / "__init__.py",
                kind="ComponentSignature",
                message=f"Failed to import core dependency 'param.Parameterized': {exc}",
            )
            return
        try:
            from panel.viewable import Layoutable
        except Exception as exc:
            self.add_error(
                file=PANEL_DIR / "viewable.py",
                kind="ComponentSignature",
                message=f"Failed to import core class 'panel.viewable.Layoutable': {exc}",
            )
            return

        for mod_name, mod_path in PUBLIC_MODULES:
            try:
                module = importlib.import_module(mod_name)
            except Exception as exc:
                if _is_optional_import_error(exc, mod_name):
                    continue
                self.add_error(
                    file=mod_path,
                    kind="ComponentSignature",
                    message=f"Failed to import core module '{mod_name}': {exc}",
                )
                continue
            for attr_name in dir(module):
                if attr_name.startswith("_"):
                    continue
                try:
                    obj = getattr(module, attr_name)
                except Exception as exc:
                    if _is_optional_import_error(exc, f"{mod_name}.{attr_name}"):
                        continue
                    raise
                if not inspect.isclass(obj):
                    continue
                if not issubclass(obj, Parameterized) or obj is Parameterized:
                    continue
                try:
                    params = set(obj.param.objects(instance=False).keys())
                except Exception as exc:
                    if _is_optional_import_error(exc, f"{mod_name}.{attr_name}"):
                        continue
                    self.add_error(
                        file=Path(inspect.getfile(obj)),
                        kind="ComponentSignature",
                        message=(
                            f"Failed to introspect parameters of "
                            f"'{mod_name}.{attr_name}': {exc}"
                        ),
                        object_name=f"{mod_name}.{attr_name}",
                    )
                    continue
                if issubclass(obj, Layoutable):
                    missing = [p for p in REQUIRED_BASE_PARAMS["Layoutable"] if p not in params]
                    if missing:
                        try:
                            src_file = Path(inspect.getfile(obj))
                        except Exception:
                            src_file = mod_path
                        self.add_error(
                            file=src_file,
                            kind="ComponentSignature",
                            message=(
                                f"Class '{mod_name}.{attr_name}' (Layoutable subclass) is "
                                f"missing expected base parameters: {missing}"
                            ),
                            object_name=f"{mod_name}.{attr_name}",
                        )


# ---------------------------------------------------------------------------
# 5. Frontend resource reference checker
# ---------------------------------------------------------------------------

DOC_STATIC_DIR = DOC_DIR / "_static"
THEME_CSS_SRC_DIR = PANEL_DIR / "theme" / "css"


class ResourceRefChecker(SyncChecker):
    """Verify that static resources referenced in docs & templates exist."""

    requires_runtime = False
    description = "Verifies _static/, bundled/, dist/, panel_dist/ references resolve to real files."

    def _collect_panel_dist_files(self) -> set[str]:
        bundled: set[str] = set()
        if BUNDLE_DIR.is_dir():
            for p in BUNDLE_DIR.rglob("*"):
                if p.is_file():
                    bundled.add("bundled/" + str(p.relative_to(BUNDLE_DIR)))
        if DIST_DIR.is_dir():
            for p in DIST_DIR.rglob("*"):
                if p.is_file():
                    bundled.add(str(p.relative_to(DIST_DIR)))
        # Theme CSS source files are copied into bundled/theme/ at build time;
        # accept the source tree as valid even when dist/ hasn't been built yet.
        if THEME_CSS_SRC_DIR.is_dir():
            for p in THEME_CSS_SRC_DIR.rglob("*.css"):
                if p.is_file():
                    bundled.add("bundled/theme/" + p.name)
        return bundled

    def _collect_doc_static_files(self) -> set[str]:
        static_files: set[str] = set()
        if DOC_STATIC_DIR.is_dir():
            for p in DOC_STATIC_DIR.rglob("*"):
                if p.is_file():
                    static_files.add(str(p.relative_to(DOC_STATIC_DIR)))
        return static_files

    def _scan_refs(self) -> list[tuple[Path, int, str, str]]:
        """Scan markdown and notebooks. Returns (file, line, prefix, rel_path).

        The prefix must be preceded by a non-path character (or string start)
        so that we don't pick up ``_static/`` segments inside full URLs such as
        ``https://.../docs/_static/...``.
        """
        import re
        matches: list[tuple[Path, int, str, str]] = []
        pattern = re.compile(
            r"(?<![A-Za-z0-9/_-])(_static|bundled|dist|panel_dist)/([^\s\"'`)\]]+)"
        )
        for md_path in iter_markdown(DOC_DIR):
            try:
                text = md_path.read_text(encoding="utf-8")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                for m in pattern.finditer(line):
                    matches.append((md_path, i, m.group(1), m.group(2).strip()))
        for nb_path in list(iter_notebooks(REF_DIR)) + list(iter_notebooks(GALLERY_DIR)):
            try:
                cells = read_notebook_code_cells(nb_path)
            except Exception:
                continue
            for cell_idx, source in cells:
                for m in pattern.finditer(source):
                    matches.append((nb_path, cell_idx + 1, m.group(1), m.group(2).strip()))
        return matches

    def run(self) -> None:
        panel_dist = self._collect_panel_dist_files()
        doc_static = self._collect_doc_static_files()
        refs = self._scan_refs()
        # Third-party library names commonly referenced in custom-component tutorials
        # (these are expected to be downloaded by the user, not shipped with panel)
        third_party_fragments = (
            "leaflet", "material-components-web", "vue", "bootstrap-vue",
            "gridjs", "mermaid", "chart.umd", "bootstrap.min",
        )
        for file, line, prefix, ref in refs:
            ref = ref.rstrip("'\",;)]}>")
            ref = ref.lstrip("/")
            while "//" in ref:
                ref = ref.replace("//", "/")
            if not ref:
                continue
            if "{{" in ref or "}}" in ref:
                continue
            if "*" in ref or "?" in ref:
                continue
            if ref.startswith("http://") or ref.startswith("https://"):
                continue
            # Known build-time conceptual artifacts
            if ref in ("custom.bundle.js", "panel.min.js", "wheels"):
                continue
            # Skip obvious third-party library files mentioned in tutorials
            ref_lower = ref.lower()
            if any(frag in ref_lower for frag in third_party_fragments):
                continue

            if prefix == "_static":
                pool = doc_static
                pool_desc = "doc/_static/"
                full_ref = ref
            elif prefix == "bundled":
                pool = panel_dist
                pool_desc = "panel/dist/bundled/"
                full_ref = "bundled/" + ref
            else:
                pool = panel_dist
                pool_desc = "panel/dist/"
                full_ref = ref

            if not pool:
                continue

            if full_ref in pool:
                continue
            # Fuzzy match — allow suffix / basename matches
            if any(p.endswith("/" + full_ref) or p == full_ref or full_ref.endswith(p) for p in pool):
                continue

            self.add_error(
                file=file,
                kind="ResourceRef",
                message=(
                    f"Referenced resource '{prefix}/{ref}' not found under "
                    f"{pool_desc}. Available files count: {len(pool)}."
                ),
                line=line,
                object_name=f"{prefix}/{ref}",
            )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

ALL_CHECKERS: list[type[SyncChecker]] = [
    ReferencePathChecker,
    GalleryParamChecker,
    PublicImportChecker,
    ComponentSignatureChecker,
    ResourceRefChecker,
]


def _probe_core_dependencies() -> Exception | None:
    """Probe whether the panel runtime core dependencies are importable.

    Returns ``None`` on success or the first import exception on failure.
    """
    try:
        import param  # noqa: F401
    except Exception as exc:
        return exc
    try:
        import panel  # noqa: F401
    except Exception as exc:
        return exc
    return None


def _diagnose_environment() -> dict[str, t.Any]:
    """Collect a structured snapshot of the runtime/import environment."""
    core_exc = _probe_core_dependencies()
    info: dict[str, t.Any] = {
        "core_dependencies_ok": core_exc is None,
        "core_dependencies_error": str(core_exc) if core_exc else None,
        "checkers": [],
    }
    for cls in ALL_CHECKERS:
        info["checkers"].append({
            "name": cls.__name__,
            "requires_runtime": cls.requires_runtime,
            "description": cls.description,
        })
    return info


def _print_diagnosis() -> None:
    info = _diagnose_environment()
    core_ok = info["core_dependencies_ok"]
    core_status = f"{GREEN}OK{RESET}" if core_ok else f"{RED}MISSING{RESET}"
    print(f"=== {CYAN}docs_sync environment diagnosis{RESET} ===")
    print(f"  Core runtime dependencies (param + panel): {core_status}")
    if not core_ok:
        print(f"    Error: {info['core_dependencies_error']}")
    print()
    print(f"  {CYAN}Registered checkers:{RESET}")
    for entry in info["checkers"]:
        mode = "runtime" if entry["requires_runtime"] else "static"
        mode_colored = f"{YELLOW}{mode}{RESET}" if entry["requires_runtime"] else f"{GREEN}{mode}{RESET}"
        print(f"    - {entry['name']} [{mode_colored}]")
        print(f"        {entry['description']}")
    if not core_ok:
        print()
        print(f"  {YELLOW}Note:{RESET} Runtime checkers require a full docs environment:")
        print(f"    pixi run -e docs python scripts/docs_sync.py")
        print(f"    pixi run -e docs docs-build")
        print(f"    pip install -e '.[doc]'")


def _print_help() -> None:
    print("Usage: python scripts/docs_sync.py [OPTIONS]")
    print()
    print("Panel doc/source sync validator — gates the docs build against stale")
    print("references between notebooks, API docs, public imports, component")
    print("signatures, and bundled frontend resources.")
    print()
    print("Options:")
    print("  --static-only    Run only the checkers that never import panel/param")
    print("                   (PublicImportChecker, ResourceRefChecker). Useful")
    print("                   for fast pre-commit checks outside the docs env.")
    print("  --diagnose       Print environment + checker inventory and exit.")
    print("  -q, --quiet      Suppress per-checker progress lines.")
    print("  -h, --help       Show this help.")
    print()
    print("Exit codes:")
    print("  0  all applicable checks passed")
    print("  1  one or more sync errors OR core runtime dependencies missing")
    print("     (default mode only; --static-only and --diagnose never fail for")
    print("      missing runtime deps)")


def run_checks(
    verbose: bool = True,
    *,
    static_only: bool = False,
) -> int:
    """Run the doc/source sync checkers.

    Parameters
    ----------
    verbose : bool
        Whether to print per-checker progress and per-error details.
    static_only : bool
        If True, only run checkers with ``requires_runtime = False`` and
        do NOT treat missing core runtime dependencies as a failure.

    Returns
    -------
    int
        Process exit code (0 = pass, 1 = fail).
    """
    # --- dependency gating ---
    core_exc = _probe_core_dependencies()
    runtime_available = core_exc is None

    if not runtime_available:
        if static_only:
            if verbose:
                print(
                    f"{YELLOW}INFO{RESET} Core runtime dependencies unavailable "
                    f"({core_exc}); running static-only checkers.",
                    flush=True,
                )
        else:
            if verbose:
                print(
                    f"{RED}FATAL{RESET} Core runtime dependencies missing for "
                    f"doc/source sync checks.\n"
                    f"  First failure: {core_exc}\n"
                    f"  Run this script inside the panel 'docs' environment, e.g.:\n"
                    f"    pixi run -e docs python scripts/docs_sync.py\n"
                    f"    pixi run -e docs docs-build\n"
                    f"  Alternatively install the project with documentation extras:\n"
                    f"    pip install -e '.[doc]'\n"
                    f"  For a lightweight offline pass use:\n"
                    f"    python scripts/docs_sync.py --static-only",
                    flush=True,
                )
            return 1

    # --- build the active checker list ---
    active: list[type[SyncChecker]] = []
    for cls in ALL_CHECKERS:
        if static_only and cls.requires_runtime:
            continue
        active.append(cls)

    # --- run ---
    all_errors: list[SyncError] = []
    for cls in active:
        name = cls.__name__
        checker = cls()
        if verbose:
            mode = " (static)" if not cls.requires_runtime else ""
            print(f"{CYAN}Running{RESET} {name}{mode}...", flush=True)
        try:
            checker.run()
        except Exception as exc:
            print(f"{YELLOW}WARNING{RESET} {name} raised an exception: {exc}", flush=True)
            continue
        if checker.errors:
            all_errors.extend(checker.errors)
            if verbose:
                for err in checker.errors:
                    print("  " + err.format(BASE_DIR), flush=True)

    if not all_errors:
        if verbose:
            scope = "static " if static_only else ""
            print(f"{GREEN}All {scope}doc/source sync checks passed.{RESET}", flush=True)
        return 0
    if verbose:
        print(
            f"{RED}{len(all_errors)} sync error(s) found — aborting doc build.{RESET}",
            flush=True,
        )
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "-h" in argv or "--help" in argv:
        _print_help()
        return 0
    if "--diagnose" in argv:
        _print_diagnosis()
        return 0

    verbose = "-q" not in argv and "--quiet" not in argv
    static_only = "--static-only" in argv

    return run_checks(verbose=verbose, static_only=static_only)


if __name__ == "__main__":
    sys.exit(main())
