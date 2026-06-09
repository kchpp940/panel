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

    def run(self, runtime_available: bool) -> None:  # noqa: ARG002
        """Execute the checker.

        Parameters
        ----------
        runtime_available : bool
            True when param + panel are importable.  Checkers that declare
            ``requires_runtime = True`` can assume this is always True when
            invoked from the default (non-``--static-only``) pipeline; the
            flag is still passed so hybrid checkers can downgrade gracefully
            in ``--static-only`` runs.
        """
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

    def run(self, runtime_available: bool) -> None:
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

# Public module roots that we know how to resolve components out of
# once alias expansion is done.
PN_COMPONENT_PREFIXES: tuple[str, ...] = (
    "panel.",
    "pn.",
)

# Root dotted names that, after alias expansion, are treated as "direct"
# component class references.  Used so `Select(...)` imported via
# `from panel.widgets import Select` is treated the same way as
# `pn.widgets.Select(...)`.
_PANEL_MODULE_ROOTS: tuple[str, ...] = (
    "panel",
    "panel.widgets",
    "panel.pane",
    "panel.layout",
    "panel.template",
    "panel.chat",
    "panel.param",
    "panel.viewable",
)

# Attributes commonly chained off panel / pn that are not component constructors.
_NON_COMPONENT_ATTRS: frozenset[str] = frozenset({
    "pn.extension", "panel.extension",
    "pn.config", "panel.config",
    "pn.state", "panel.state",
    "pn.pipeline", "panel.pipeline",
    "pn.io", "panel.io",
    "pn.param", "panel.param",
    "pn.panel", "panel.panel",
    "pn.Row", "pn.Column", "pn.GridSpec", "pn.Tabs",
    "pn.Spacer", "pn.HSpacer", "pn.VSpacer",
})


def _resolve_component_class(attr_chain: str) -> tuple[type | None, Exception | None]:
    """Resolve a dotted name like 'panel.widgets.Select' to the actual class.

    The input is expected to already have alias expansion performed by the
    notebook visitor, so both ``panel.widgets.Select`` and the legacy
    ``pn.widgets.Select`` forms are accepted, as well as a bare
    ``Select`` when the name directly resolves to a component type.

    Returns ``(cls_or_None, exc_or_None)``. The second element is only
    populated when a **core** (non-optional) import failed so the caller
    can surface it.
    """
    import importlib

    parts = attr_chain.split(".")
    if not parts:
        return None, None

    # Normalize the legacy shorthand.
    if parts[0] == "pn":
        parts[0] = "panel"

    # Single name (e.g. bare ``Select`` after a `from panel.widgets import Select`).
    # Try a direct getattr on each known public root.
    if len(parts) == 1:
        name = parts[0]
        for root in _PANEL_MODULE_ROOTS:
            try:
                module = importlib.import_module(root)
            except Exception as exc:
                if _is_optional_import_error(exc, root):
                    continue
                return None, exc
            try:
                obj = getattr(module, name)
            except Exception as exc:
                if _is_optional_import_error(exc, f"{root}.{name}"):
                    continue
                return None, exc
            if isinstance(obj, type):
                return obj, None
        return None, None

    # Multi-part dotted path.  Import the module, then getattr the final segments.
    # Walk progressively longer module prefixes so that 'panel.widgets.Select' works
    # whether 'panel.widgets' itself is the module or 'panel' is the module that
    # re-exports 'widgets.Select' as an attribute.
    last_exc: Exception | None = None
    for split_idx in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split_idx])
        attr_segments = parts[split_idx:]
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            if _is_optional_import_error(exc, module_name):
                continue
            last_exc = exc
            continue
        obj: t.Any = module
        ok = True
        for seg in attr_segments:
            try:
                obj = getattr(obj, seg)
            except Exception as exc:
                full_so_far = ".".join(parts[:split_idx] + [seg])
                if _is_optional_import_error(exc, full_so_far):
                    ok = False
                    break
                last_exc = exc
                ok = False
                break
        if not ok:
            continue
        if isinstance(obj, type):
            return obj, None
    return None, last_exc


# ---------------------------------------------------------------------------
# Alias-aware AST visitor for notebook code cells
# ---------------------------------------------------------------------------

class _ComponentInstantiationVisitor(ast.NodeVisitor):
    """Walk a single notebook code cell.

    * Records every ``import`` / ``from ... import`` so that local aliases
      such as ``pn``, ``pnw``, ``pw`` or bare class names imported directly
      can be resolved back to fully-qualified dotted names.
    * Collects every ``Call`` node whose callee resolves to a panel
      component together with the keyword argument names passed to it.
    """

    def __init__(self) -> None:
        # (resolved_dotted_chain, keyword_names, line_no)
        self.calls: list[tuple[str, list[str], int]] = []
        # name -> fully qualified dotted prefix
        #   'pn'  -> 'panel'
        #   'pnw' -> 'panel.widgets'
        #   'pw'  -> 'panel.widgets'
        #   'Select' -> 'panel.widgets.Select'
        self._aliases: dict[str, str] = {}

    # ----- import tracking -----

    def visit_Import(self, node: ast.Import) -> None:
        # ``import panel [as pn]``, ``import panel.widgets [as pnw]``
        for alias in node.names:
            local_name = alias.asname if alias.asname else alias.name.split(".")[0]
            self._aliases[local_name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # ``from panel.widgets import Select [as S]``
        # ``from panel import widgets [as pw]``
        if node.module is None:
            self.generic_visit(node)
            return
        # Resolve relative imports (unlikely in notebooks, but harmless)
        level = node.level or 0
        if level > 0:
            self.generic_visit(node)
            return
        module = node.module
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname if alias.asname else alias.name
            self._aliases[local_name] = f"{module}.{alias.name}"
        self.generic_visit(node)

    # ----- call collection -----

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        func = node.func
        chain = self._resolve_call_to_chain(func)
        if chain is None:
            return
        # Ignore obvious non-component helpers.
        if chain in _NON_COMPONENT_ATTRS:
            return
        # We only want panel components — accept 'panel.XXX' or names that
        # resolve through the known import aliases that point to panel.*.
        if not (
            chain.startswith("panel.")
            or chain.startswith("pn.")
            or any(chain.startswith(root + ".") for root in _PANEL_MODULE_ROOTS)
            or any(chain == self._aliases.get(k) for k in self._aliases)
        ):
            # Still allow any bare class name imported from panel.*
            if "." not in chain:
                for v in self._aliases.values():
                    if v == chain and v.startswith("panel."):
                        break
                else:
                    return
            else:
                return
        kwargs = [kw.arg for kw in node.keywords if kw.arg is not None]
        self.calls.append((chain, kwargs, node.lineno))

    # ----- helpers -----

    def _resolve_call_to_chain(self, func: ast.AST) -> str | None:
        """Return the fully-qualified dotted name for a callee AST node.

        Examples with tracked aliases::

            pn.widgets.Select     ->  panel.widgets.Select     (alias 'pn' -> 'panel')
            pnw.Select            ->  panel.widgets.Select     (alias 'pnw' -> 'panel.widgets')
            pw.Select             ->  panel.widgets.Select     (alias 'pw'  -> 'panel.widgets')
            Select                ->  panel.widgets.Select     (alias 'Select' -> 'panel.widgets.Select')
            panel.widgets.Select  ->  panel.widgets.Select     (no alias needed)
        """
        # Walk from the right-most attribute, collecting attribute names.
        attr_parts: list[str] = []
        cur = func
        while isinstance(cur, ast.Attribute):
            attr_parts.append(cur.attr)
            cur = cur.value
        attr_parts.reverse()
        # The left-most node must be a simple Name we can look up.
        if not isinstance(cur, ast.Name):
            return None
        root_name = cur.id
        # Case 1: bare name call, e.g.  Select(...)
        if not attr_parts:
            if root_name in self._aliases:
                return self._aliases[root_name]
            return root_name
        # Case 2: attribute chain, e.g.  pn.widgets.Select(...) or pnw.Select(...)
        if root_name in self._aliases:
            prefix = self._aliases[root_name]
            return prefix + "." + ".".join(attr_parts)
        # No alias — trust the name exactly as written (covers `panel.widgets.Select`).
        return root_name + "." + ".".join(attr_parts)


class GalleryParamChecker(SyncChecker):
    """Check that gallery & reference notebooks do not reference deleted parameters.

    The visitor tracks **every** import form commonly used in Panel tutorials:

    * ``import panel as pn``            → resolves ``pn.widgets.Select``
    * ``import panel.widgets as pnw``   → resolves ``pnw.Select``
    * ``from panel import widgets as pw``  → resolves ``pw.Select``
    * ``from panel.widgets import Select`` → resolves bare ``Select(...)``
    * ``import panel``                  → resolves ``panel.widgets.Select``
    """

    requires_runtime = True
    description = "Scans notebook code cells (alias-aware) and validates keyword arg names against component signatures."

    def run(self, runtime_available: bool) -> None:
        targets: list[Path] = []
        if GALLERY_DIR.is_dir():
            targets.extend(iter_notebooks(GALLERY_DIR))
        if REF_DIR.is_dir():
            targets.extend(iter_notebooks(REF_DIR))

        # Avoid reporting the same broken core import for every single notebook / cell.
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
                except SyntaxError:
                    # Notebooks often contain partial code cells / magics; skip silently.
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
                                f"Cell #{cell_idx + 1}: Failed to resolve core "
                                f"component '{chain}': {core_exc}"
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
                                    f"Cell #{cell_idx + 1}: Failed to introspect "
                                    f"parameters of '{chain}': {exc}"
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
                                    f"Cell #{cell_idx + 1}: Parameter '{kw}' passed "
                                    f"to '{chain}' does not exist on the component. "
                                    f"Known params: {sorted(valid_params)[:10]}..."
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
    """Verify that every name declared in ``__all__`` is actually exposed by
    the public module.

    In the default (runtime-available) mode each ``__all__`` entry is checked
    via a real ``hasattr(module, name)`` call — the same path a docs build or
    end-user import would take.  When running with ``--static-only`` the
    checker falls back to a pure AST analysis that collects top-level
    bindings (definitions, imports, assignments) and additionally recognises
    lazy ``__getattr__`` exports for entries that appear in the explicit
    optional-dependency whitelist.
    """

    requires_runtime = False
    description = "Validates __all__ against real module attributes (runtime) or AST bindings (--static-only)."

    # Names implicitly available on every module; never flagged as missing.
    _IMPLICIT_NAMES: t.ClassVar[frozenset[str]] = frozenset({
        "__name__", "__doc__", "__package__", "__loader__", "__spec__",
        "__file__", "__cached__", "__builtins__", "__path__",
        "__all__", "__version__",
    })

    # ------------------------------------------------------------------
    # AST helpers (used only in --static-only mode, and kept here for
    # environments where the panel runtime cannot be imported at all).
    # ------------------------------------------------------------------
    @classmethod
    def _collect_top_level_bindings(cls, tree: ast.Module) -> set[str]:
        """Return all names bound at the top level of an AST module."""
        bindings: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings.add(node.name)
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bindings.add(alias.asname if alias.asname else alias.name.split(".")[0])
                continue
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        bindings.add("*")
                    else:
                        bindings.add(alias.asname if alias.asname else alias.name)
                continue
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    bindings.update(cls._extract_assigned_names(tgt))
                continue
            if isinstance(node, ast.NamedExpr):
                bindings.update(cls._extract_assigned_names(node.target))
                continue
            if isinstance(node, (ast.For, ast.AsyncFor)):
                bindings.update(cls._extract_assigned_names(node.target))
                continue
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
        _walk(node)
        return names

    @classmethod
    def _has_lazy_getattr(cls, tree: ast.Module) -> bool:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "__getattr__":
                    return True
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "__getattr__":
                        return True
        return False

    @classmethod
    def _extract_all_names(cls, tree: ast.Module) -> list[str]:
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__all__"
                and isinstance(node.value, (ast.Tuple, ast.List))
            ):
                return [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
        return []

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self, runtime_available: bool) -> None:
        import importlib

        for mod_name, mod_path in PUBLIC_MODULES:
            if not mod_path.is_file():
                continue

            # --- Always parse source: we need __all__ contents and, in
            #     static-only mode, the binding set.
            try:
                tree = ast.parse(mod_path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                self.add_error(
                    file=mod_path,
                    kind="PublicImport",
                    message=f"Failed to parse module source: {exc}",
                )
                continue

            all_names = self._extract_all_names(tree)
            if not all_names:
                continue

            # --- Runtime path: real hasattr() — the authoritative check. ---
            if runtime_available:
                try:
                    module = importlib.import_module(mod_name)
                except Exception as exc:
                    if _is_optional_import_error(exc, mod_name):
                        continue
                    self.add_error(
                        file=mod_path,
                        kind="PublicImport",
                        message=f"Failed to import core module '{mod_name}': {exc}",
                    )
                    continue

                for name in all_names:
                    full_dotted = f"{mod_name}.{name}"
                    try:
                        present = hasattr(module, name)
                    except Exception as exc:
                        if _is_optional_import_error(exc, full_dotted):
                            continue
                        self.add_error(
                            file=mod_path,
                            kind="PublicImport",
                            message=(
                                f"hasattr('{mod_name}', '{name}') raised: {exc}"
                            ),
                            object_name=name,
                        )
                        continue
                    if not present:
                        self.add_error(
                            file=mod_path,
                            kind="PublicImport",
                            message=(
                                f"'{mod_name}.{name}' is declared in __all__ "
                                f"but hasattr returned False in the docs runtime."
                            ),
                            object_name=name,
                        )
                continue

            # --- Static-only fallback: AST bindings + lazy whitelist. ---
            bindings = self._collect_top_level_bindings(tree)
            has_lazy = self._has_lazy_getattr(tree)
            for name in all_names:
                if name in bindings or name in self._IMPLICIT_NAMES:
                    continue
                if "*" in bindings:
                    continue
                full_dotted = f"{mod_name}.{name}"
                if any(root == full_dotted or full_dotted.startswith(root + ".")
                       for root in OPTIONAL_DEPENDENCY_ROOTS):
                    continue
                if has_lazy:
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
                        f"definition or import in '{mod_name}' (--static-only mode)."
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

    def run(self, runtime_available: bool) -> None:
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

    def run(self, runtime_available: bool) -> None:
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
            checker.run(runtime_available=runtime_available)
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
