from __future__ import annotations

import argparse
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
    print("\n[1/6] Checking TypeScript models vs built JS...")

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
    print("\n[2/6] Checking CSS files in panel/dist/css/...")
    css_dir = DIST_DIR / "css"
    if not css_dir.exists():
        _print_error("panel/dist/css/ directory does not exist")
        errors.append("panel/dist/css/ directory does not exist")
        return

    css_files = list(css_dir.glob("*.css"))
    if not css_files:
        _print_error("No CSS files found in panel/dist/css/")
        errors.append("No CSS files found in panel/dist/css/")
        return

    _print_ok(f"Found {len(css_files)} CSS files")


def check_bundled_resources(errors: list[str]) -> None:
    print("\n[3/6] Checking bundled resources referenced by Python code...")

    if not BUNDLE_DIR.exists():
        _print_error("panel/dist/bundled/ does not exist (bundled resources have not been built)")
        errors.append("panel/dist/bundled/ does not exist")
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
    print("\n[4/6] Checking for orphaned/outdated source maps...")

    if not DIST_DIR.exists():
        _print_error("panel/dist/ does not exist")
        errors.append("panel/dist/ does not exist")
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
            _print_error(f"Orphaned source map (stale): {m.relative_to(ROOT)}")
            errors.append(f"Orphaned source map: {m.relative_to(ROOT)}")
    else:
        _print_ok(f"All {len(map_files)} source maps have corresponding sources")


def check_package_json_and_python_package_data(errors: list[str]) -> None:
    print("\n[5/6] Checking package.json and Python package data configuration...")

    if not PACKAGE_JSON.exists():
        _print_error(f"package.json not found at {PACKAGE_JSON}")
        errors.append("package.json not found")
    else:
        pkg = json.loads(PACKAGE_JSON.read_text())
        files_patterns = pkg.get("files", [])
        if not files_patterns:
            _print_warn("package.json files field is empty")
        else:
            _print_ok(f"package.json files field patterns: {files_patterns}")

        main_field = pkg.get("main", "")
        if main_field:
            main_path = PANEL_DIR / main_field
            if not main_path.exists():
                _print_error(f"package.json main entry '{main_field}' does not exist at {main_path.relative_to(ROOT)}")
                errors.append(f"package.json main entry '{main_field}' missing")
            else:
                _print_ok(f"package.json main entry '{main_field}' exists")

    if not PYPROJECT.exists():
        _print_error(f"pyproject.toml not found at {PYPROJECT}")
        errors.append("pyproject.toml not found")
    else:
        _print_ok("pyproject.toml exists")

    if not DIST_DIR.exists():
        _print_error("panel/dist/ does not exist")
        errors.append("panel/dist/ does not exist")
    else:
        dist_files = [f for f in DIST_DIR.rglob("*") if f.is_file()]
        if not dist_files:
            _print_error("panel/dist/ contains no files")
            errors.append("panel/dist/ contains no files")
        else:
            _print_ok(f"panel/dist/ contains {len(dist_files)} files")


def check_wheel_contents(errors: list[str], wheel_path: Path) -> None:
    print("\n[6/6] Checking wheel contents...")

    if not wheel_path.exists():
        _print_error(f"Wheel not found at {wheel_path}")
        errors.append(f"Wheel not found: {wheel_path}")
        return

    _print_ok(f"Checking wheel: {wheel_path.name}")

    with zipfile.ZipFile(wheel_path) as zf:
        wheel_files = set(zf.namelist())

    panel_dist_files: set[str] = set()
    if DIST_DIR.exists():
        for f in DIST_DIR.rglob("*"):
            if f.is_file():
                rel = f.relative_to(PANEL_DIR)
                panel_dist_files.add(str(rel).replace(os.sep, "/"))

    if not panel_dist_files:
        _print_error("panel/dist/ has no files to compare against wheel")
        errors.append("panel/dist/ is empty, cannot verify wheel contents")
        return

    missing_in_wheel = sorted(panel_dist_files - wheel_files)

    if missing_in_wheel:
        for mf in missing_in_wheel[:20]:
            _print_error(f"  Missing from wheel: {mf}")
        if len(missing_in_wheel) > 20:
            _print_error(f"  ... and {len(missing_in_wheel) - 20} more")
        errors.append(f"{len(missing_in_wheel)} files from panel/dist/ are missing from wheel {wheel_path.name}")
    else:
        _print_ok(f"  All {len(panel_dist_files)} panel/dist/ files present in wheel")

    extra_in_wheel = sorted({
        wf for wf in wheel_files
        if wf.startswith("panel/dist/") and wf not in panel_dist_files and not wf.endswith("/")
    })
    if extra_in_wheel:
        for ef in extra_in_wheel[:10]:
            _print_warn(f"  Extra in wheel (stale?): {ef}")
        if len(extra_in_wheel) > 10:
            _print_warn(f"  ... and {len(extra_in_wheel) - 10} more")


def run_source_and_dist_checks() -> list[str]:
    """Run all source/dist checks (Phase 1). Always strict."""
    errors: list[str] = []
    check_models_ts_vs_built_js(errors)
    check_css_files(errors)
    check_bundled_resources(errors)
    check_sourcemaps(errors)
    check_package_json_and_python_package_data(errors)
    return errors


def run_wheel_check(wheel_path: Path) -> list[str]:
    """Run wheel contents check (Phase 2). Strict when called."""
    errors: list[str] = []
    check_wheel_contents(errors, wheel_path)
    return errors


def find_unique_wheel() -> tuple[Path | None, list[str]]:
    """Locate exactly one wheel in dist/. Returns (wheel_path, errors).

    Fails if 0 or 2+ wheels are present, preventing ambiguous release verification.
    """
    errors: list[str] = []
    dist_root = ROOT / "dist"
    if not dist_root.exists():
        errors.append(f"dist/ directory not found at {dist_root}")
        return None, errors

    wheels = sorted(dist_root.glob("*.whl"))
    if len(wheels) == 0:
        errors.append("No .whl files found in dist/ — build must complete before release verification")
        return None, errors
    if len(wheels) > 1:
        names = ", ".join(w.name for w in wheels)
        errors.append(
            f"Multiple .whl files found in dist/ ({len(wheels)}): {names} "
            "— clean dist/ and rebuild to avoid ambiguous verification"
        )
        return None, errors

    return wheels[0], errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Panel frontend build artifacts are consistent."
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help="Path to the built .whl file. If provided, also verifies wheel contents.",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        default=False,
        help="Release-mode verification: run source/dist checks, then locate exactly one wheel "
             "in dist/ and verify its contents. Fails if 0 or 2+ wheels are found.",
    )
    args = parser.parse_args()

    if args.release and args.wheel:
        print(f"{RED}ERROR{RESET}: --release and --wheel are mutually exclusive. "
              "Use --release for auto-detection, or --wheel <path> for an explicit path.")
        return 1

    print("=" * 70)
    wheel_path: Path | None = None
    mode_label = "SOURCE/DIST ONLY"
    find_errors: list[str] = []

    if args.release:
        mode_label = "RELEASE (auto-locate wheel in dist/)"
        wheel_path, find_errors = find_unique_wheel()
        if wheel_path is not None:
            mode_label = f"RELEASE (wheel: {wheel_path.name})"
    elif args.wheel:
        wheel_path = args.wheel
        mode_label = f"WHEEL (explicit path: {wheel_path})"

    print(f"Panel Frontend Artifact Verification — {mode_label}")
    print("=" * 70)

    errors = run_source_and_dist_checks()
    errors.extend(find_errors)

    if wheel_path is not None and not find_errors:
        wheel_errors = run_wheel_check(wheel_path)
        errors.extend(wheel_errors)

    print("\n" + "=" * 70)
    if errors:
        _print_error(f"Verification FAILED with {len(errors)} issue(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    else:
        if wheel_path is not None:
            _print_ok("All source/dist and wheel checks passed!")
        else:
            _print_ok("All source/dist checks passed! (wheel not checked)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
