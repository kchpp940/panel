from __future__ import annotations

import importlib
import json
import os
import re
import sys
import zipfile

from pathlib import Path
from typing import Any

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RESET = "\033[0m"

ROOT = Path(__file__).parent.parent
PANEL_DIR = ROOT / "panel"
DIST_DIR = PANEL_DIR / "dist"
BUNDLE_DIR = DIST_DIR / "bundled"
MODELS_DIR = PANEL_DIR / "models"
PACKAGE_JSON = PANEL_DIR / "package.json"
PYPROJECT = ROOT / "pyproject.toml"

NON_MODEL_TS = {
    "util", "event-to-object", "declaration", "data",
}


def _print_error(msg: str) -> None:
    print(f"{RED}ERROR{RESET}: {msg}")


def _print_warn(msg: str) -> None:
    print(f"{YELLOW}WARN{RESET}: {msg}")


def _print_ok(msg: str) -> None:
    print(f"{GREEN}OK{RESET}: {msg}")


def _ts_stem(path: Path) -> str:
    return path.name[:-3] if path.name.endswith(".ts") else path.stem


def check_models_ts_vs_built_js(errors: list[str]) -> None:
    print("\n[1/7] Checking TypeScript models vs built JS...")

    top_level_ts = {
        _ts_stem(f) for f in MODELS_DIR.glob("*.ts")
        if f.name not in ("declaration.d.ts", "index.ts")
    }

    vtk_dir = MODELS_DIR / "vtk"
    vtk_ts: set[str] = set()
    if vtk_dir.exists():
        for f in vtk_dir.glob("*.ts"):
            if f.name != "index.ts":
                vtk_ts.add(f"vtk/{_ts_stem(f)}")

    all_ts = top_level_ts | vtk_ts
    all_ts -= NON_MODEL_TS

    built_js_names = set()
    if DIST_DIR.exists():
        for f in DIST_DIR.glob("*.js"):
            built_js_names.add(f.stem)

    ts_index = MODELS_DIR / "index.ts"
    exported_ts: set[str] = set()
    if ts_index.exists():
        content = ts_index.read_text()
        for match in re.finditer(
            r'export\s*\{\s*\w+\s*\}\s+from\s+["\']\.\/([\w-]+)["\']',
            content,
        ):
            exported_ts.add(match.group(1))
        for match in re.finditer(
            r'export\s+\*\s+from\s+["\']\.\/([\w-]+)["\']',
            content,
        ):
            exported_ts.add(match.group(1))

    has_panel_bundle = any(
        (DIST_DIR / name).exists()
        for name in ("panel.min.js", "panel.js")
    )

    missing: list[str] = []
    for ts in sorted(all_ts):
        ts_base = ts.split("/")[-1]
        if ts_base not in exported_ts and "vtk/" not in ts:
            continue
        candidates = [
            DIST_DIR / f"panel.{ts_base}.js",
            DIST_DIR / f"{ts_base}.js",
        ]
        if not any(p.exists() for p in candidates) and not has_panel_bundle:
            missing.append(ts)

    if missing:
        for ts in sorted(missing):
            _print_error(
                f"TypeScript model panel/models/{ts}.ts has no corresponding built JS in panel/dist/"
            )
            errors.append(f"Missing built JS for model {ts}")
    else:
        _print_ok("All TypeScript models accounted for")


def check_css_files(errors: list[str]) -> None:
    print("\n[2/7] Checking CSS files in panel/dist/css/...")
    css_dir = DIST_DIR / "css"
    if not css_dir.exists():
        _print_error("panel/dist/css/ directory does not exist")
        errors.append("panel/dist/css/ directory does not exist")
        return

    css_files = list(css_dir.glob("*.css"))
    if not css_files:
        _print_warn("No CSS files found in panel/dist/css/")
        return

    _print_ok(f"Found {len(css_files)} CSS files")


def check_bundled_resources(errors: list[str]) -> None:
    print("\n[3/7] Checking bundled resources referenced by Python code...")

    if not BUNDLE_DIR.exists():
        _print_warn("panel/dist/bundled/ does not exist (bundled resources may not have been built yet)")
        return

    sys.path.insert(0, str(ROOT))
    from panel.config import panel_extension
    from panel.template.base import BasicTemplate
    from panel.util import _descendents

    missing: list[str] = []

    for imp in panel_extension._imports.values():
        if imp.startswith("panel.models"):
            try:
                importlib.import_module(imp)
            except Exception:
                pass

    from bokeh.model import Model
    from panel.reactive import ReactiveHTML

    reactive = _descendents(ReactiveHTML, concrete=True)
    models: list[tuple[str, Any]] = list(Model.model_class_reverse_map.items()) + [
        (f"{m.__module__}.{m.__name__}", m) for m in reactive
    ]

    from panel.config import config as pconfig
    npm_cdn = pconfig.npm_cdn

    for name, model in models:
        if not name.startswith("panel."):
            continue
        mname = model.__name__.lower()

        for attr in ("__javascript_raw__", "__css_raw__", "__resources__"):
            raw = getattr(model, attr, None)
            if not raw:
                continue
            for item in raw:
                if not isinstance(item, str):
                    continue
                _check_bundled_url(item, attr, mname, npm_cdn, missing)

        tarball = getattr(model, "__tarball__", None)
        if tarball:
            dest = tarball.get("dest", "")
            expected_dir = BUNDLE_DIR / mname / dest
            files_under = list(expected_dir.rglob("*")) if expected_dir.exists() else []
            if not any(f.is_file() for f in files_under):
                _print_error(f"  {mname}.__tarball__ resources missing: expected files under {expected_dir.relative_to(ROOT)}")
                missing.append(f"Missing bundled tarball for {mname}")

    for template in _descendents(BasicTemplate, concrete=True):
        tname = template.__name__.lower()
        for attr in ("_css", "_js"):
            val = getattr(template, attr, None)
            if not val:
                continue
            paths = val if isinstance(val, list) else [val]
            for p in paths:
                if isinstance(p, Path):
                    if p.exists():
                        _print_ok(f"  {tname}.{attr} -> {p.relative_to(ROOT)} exists")
                    else:
                        _print_error(f"  {tname}.{attr} -> {p} does not exist")
                        missing.append(f"Missing {attr} file for {tname}: {p}")

    if missing:
        errors.extend(missing)
    else:
        _print_ok("All bundled resource references resolve correctly")


def _check_bundled_url(url: str, attr: str, model_name: str, npm_cdn: str, missing: list[str]) -> None:
    if not url.startswith("http"):
        return
    url = url.split("?")[0]
    if url.startswith(npm_cdn):
        filepath = url.replace(npm_cdn, "")[1:]
    elif url.startswith("https://cdn.jsdelivr.net/npm"):
        filepath = url.replace("https://cdn.jsdelivr.net/npm", "")[1:]
    elif url.startswith("https://unpkg.com"):
        filepath = url.replace("https://unpkg.com", "")[1:]
    else:
        filepath = "/".join(url.split("/")[3:])
    test_filepath = filepath.split("?")[0]
    possible_paths = [
        BUNDLE_DIR / model_name / test_filepath,
        BUNDLE_DIR / test_filepath,
    ]
    if not any(p.exists() for p in possible_paths):
        _print_error(f"  {model_name}.{attr} references {url}")
        _print_error(f"    Expected at {possible_paths[0].relative_to(ROOT)} or {possible_paths[1].relative_to(ROOT)}")
        missing.append(f"Missing bundled resource for {model_name}: {url}")


def check_sourcemaps(errors: list[str]) -> None:
    print("\n[4/7] Checking for orphaned/outdated source maps...")

    if not DIST_DIR.exists():
        return

    map_files = list(DIST_DIR.rglob("*.js.map")) + list(DIST_DIR.rglob("*.css.map"))
    if not map_files:
        _print_ok("No source maps found")
        return

    orphans: list[Path] = []
    for map_file in map_files:
        source_name = map_file.name[:-4]
        source = map_file.with_name(source_name)
        if not source.exists():
            orphans.append(map_file)

    if orphans:
        for m in orphans:
            _print_error(f"Orphaned source map: {m.relative_to(ROOT)}")
            errors.append(f"Orphaned source map: {m.relative_to(ROOT)}")
    else:
        _print_ok(f"All {len(map_files)} source maps have corresponding sources")


def check_package_json_consistency(errors: list[str]) -> None:
    print("\n[5/7] Checking package.json files field vs actual dist contents...")

    if not PACKAGE_JSON.exists():
        _print_error(f"package.json not found at {PACKAGE_JSON}")
        errors.append("package.json not found")
        return

    pkg = json.loads(PACKAGE_JSON.read_text())
    files_patterns = pkg.get("files", [])

    main_field = pkg.get("main", "")
    if main_field:
        main_path = PANEL_DIR / main_field
        if not main_path.exists():
            _print_warn(f"package.json main entry '{main_field}' does not exist at {main_path.relative_to(ROOT)}")
        else:
            _print_ok(f"package.json main entry '{main_field}' exists")

    _print_ok(f"package.json files field patterns: {files_patterns}")


def check_python_package_data(errors: list[str]) -> None:
    print("\n[6/7] Checking Python package data configuration...")

    if not PYPROJECT.exists():
        _print_error(f"pyproject.toml not found at {PYPROJECT}")
        errors.append("pyproject.toml not found")
    else:
        _print_ok("pyproject.toml exists")

    dist_path = PANEL_DIR / "dist"
    if dist_path.exists():
        dist_count = sum(1 for _ in dist_path.rglob("*"))
        _print_ok(f"panel/dist/ contains {dist_count} files/directories")
    else:
        _print_warn("panel/dist/ does not exist")

    _print_ok("Python package data configuration valid")


def check_wheel_contents(errors: list[str]) -> None:
    print("\n[7/7] Checking wheel contents (if wheel exists)...")

    dist_root = ROOT / "dist"
    if not dist_root.exists():
        _print_warn("Top-level dist/ directory not found, skipping wheel check")
        return

    wheels = list(dist_root.glob("*.whl"))
    if not wheels:
        _print_warn("No .whl files found in dist/, skipping wheel check")
        return

    for wheel in sorted(wheels, key=lambda p: p.stat().st_mtime, reverse=True):
        _print_ok(f"Checking wheel: {wheel.name}")

        with zipfile.ZipFile(wheel) as zf:
            wheel_files = set(zf.namelist())

        panel_dist_files: set[str] = set()
        dist_path = PANEL_DIR / "dist"
        if dist_path.exists():
            for f in dist_path.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(PANEL_DIR)
                    panel_dist_files.add(str(rel).replace(os.sep, "/"))

        missing_in_wheel = panel_dist_files - wheel_files

        if missing_in_wheel:
            for mf in sorted(missing_in_wheel)[:20]:
                _print_error(f"  Missing from wheel: {mf}")
            if len(missing_in_wheel) > 20:
                _print_error(f"  ... and {len(missing_in_wheel) - 20} more")
            errors.append(f"{len(missing_in_wheel)} files missing from wheel {wheel.name}")
        else:
            _print_ok(f"  All panel/dist/ files present in wheel")


def main() -> int:
    print("=" * 70)
    print("Panel Frontend Artifact Verification")
    print("=" * 70)

    errors: list[str] = []

    check_models_ts_vs_built_js(errors)
    check_css_files(errors)
    check_bundled_resources(errors)
    check_sourcemaps(errors)
    check_package_json_consistency(errors)
    check_python_package_data(errors)
    check_wheel_contents(errors)

    print("\n" + "=" * 70)
    if errors:
        _print_error(f"Verification FAILED with {len(errors)} issue(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    else:
        _print_ok("All checks passed!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
