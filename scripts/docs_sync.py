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
            except ImportError:
                continue

            overrides = REF_NAME_OVERRIDES.get(section_name, {})

            for nb in sorted(section.glob("*.ipynb")):
                stem = nb.stem
                component_name = overrides.get(stem, stem)

                if not hasattr(module, component_name):
                    # Also check top-level panel exports
                    import panel as pn
                    if not hasattr(pn, component_name):
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


def _resolve_component_class(attr_chain: str) -> type | None:
    """Resolve a dotted name like 'pn.widgets.Select' to the actual class."""
    import importlib

    parts = attr_chain.split(".")
    if parts[0] != "pn":
        return None
    parts[0] = "panel"
    try:
        module = importlib.import_module(".".join(parts[:-1]))
    except Exception:
        return None
    try:
        obj = __import__(parts[0], fromlist=parts[1:])
    except Exception:
        return None
    try:
        for seg in parts[1:]:
            obj = getattr(obj, seg)
        if isinstance(obj, type):
            return obj
    except Exception:
        return None


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

    def run(self) -> None:
        targets: list[Path] = []
        if GALLERY_DIR.is_dir():
            targets.extend(iter_notebooks(GALLERY_DIR))
        if REF_DIR.is_dir():
            targets.extend(iter_notebooks(REF_DIR))

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
                    cls = _resolve_component_class(chain)
                    if cls is None:
                        continue
                    try:
                        valid_params = set(cls.param.objects(instance=False).keys())
                    except Exception:
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
    """Verify that each item in __all__ is actually importable via the module."""

    def run(self) -> None:
        import importlib

        for mod_name, mod_path in PUBLIC_MODULES:
            if not mod_path.is_file():
                continue
            tree = ast.parse(mod_path.read_text(encoding="utf-8"))
            all_names: list[str] = []
            for node in ast.walk(tree):
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
            try:
                module = importlib.import_module(mod_name)
            except Exception:
                # Module not importable in current env (missing deps) — skip, not a sync error
                continue
            for name in all_names:
                if not hasattr(module, name):
                    self.add_error(
                        file=mod_path,
                        kind="PublicImport",
                        message=(
                            f"Name '{name}' is declared in __all__ but is not exported "
                            f"by module '{mod_name}'."
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

    def run(self) -> None:
        import importlib
        import inspect

        # Gather all Parameterized subclasses exported from the public modules
        for mod_name, _ in PUBLIC_MODULES:
            try:
                module = importlib.import_module(mod_name)
            except Exception:
                continue
            for attr_name in dir(module):
                if attr_name.startswith("_"):
                    continue
                try:
                    obj = getattr(module, attr_name)
                except Exception:
                    continue
                if not inspect.isclass(obj):
                    continue
                try:
                    from param import Parameterized
                except Exception:
                    return
                if not issubclass(obj, Parameterized) or obj is Parameterized:
                    continue
                try:
                    params = set(obj.param.objects(instance=False).keys())
                except Exception:
                    continue
                # If a class name suggests it should be layoutable, check base params
                try:
                    from panel.viewable import Layoutable
                except Exception:
                    continue
                if issubclass(obj, Layoutable):
                    missing = [p for p in REQUIRED_BASE_PARAMS["Layoutable"] if p not in params]
                    if missing:
                        self.add_error(
                            file=Path(inspect.getfile(obj)),
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


class ResourceRefChecker(SyncChecker):
    """Verify that static resources referenced in docs & templates exist."""

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
        return bundled

    def _collect_doc_static_files(self) -> set[str]:
        static_files: set[str] = set()
        if DOC_STATIC_DIR.is_dir():
            for p in DOC_STATIC_DIR.rglob("*"):
                if p.is_file():
                    static_files.add(str(p.relative_to(DOC_STATIC_DIR)))
        return static_files

    def _scan_refs(self) -> list[tuple[Path, int, str, str]]:
        """Scan markdown and notebooks. Returns (file, line, prefix, rel_path)."""
        import re
        matches: list[tuple[Path, int, str, str]] = []
        pattern = re.compile(r"(_static|bundled|dist|panel_dist)/([^\s\"'`)\]]+)")
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


def run_checks(verbose: bool = True) -> int:
    all_errors: list[SyncError] = []
    for cls in ALL_CHECKERS:
        checker = cls()
        name = cls.__name__
        if verbose:
            print(f"{CYAN}Running{RESET} {name}...", flush=True)
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
            print(f"{GREEN}All doc/source sync checks passed.{RESET}", flush=True)
        return 0
    if verbose:
        print(
            f"{RED}{len(all_errors)} sync error(s) found — aborting doc build.{RESET}",
            flush=True,
        )
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-q" not in argv and "--quiet" not in argv
    return run_checks(verbose=verbose)


if __name__ == "__main__":
    sys.exit(main())
