from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import typing as t
import zipfile

from .manifest import (
    AppConversionManifest,
    AssetIssue,
    AssetStatus,
    CachePolicy,
    ConsistencyReport,
    DiagnosticsSummary,
    IssueSeverity,
    LocalizationStats,
    Provenance,
    RemoteURLRef,
    WorkerType,
)


_REMOTE_URL_PATTERNS: list[tuple[str, str]] = [
    (r'https?://[^\s"\'<>)]+', 'http'),
    (r'//[^\s"\'<>)]+', 'protocol-relative'),
    (r'cdn\.jsdelivr\.net[^\s"\'<>)]*', 'jsdelivr-cdn'),
    (r'unpkg\.com[^\s"\'<>)]*', 'unpkg-cdn'),
    (r'pyscript\.net[^\s"\'<>)]*', 'pyscript-cdn'),
]

_WELL_KNOWN_CDN_HOSTS = (
    'cdn.jsdelivr.net',
    'unpkg.com',
    'pyscript.net',
    'cdn.bokeh.org',
)


@dataclasses.dataclass
class ValidationResult:
    ok: bool
    errors: list[AssetIssue] = dataclasses.field(default_factory=list)
    warnings: list[AssetIssue] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "ok": self.ok,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
        }


class ManifestValidator:
    def __init__(self, manifest: AppConversionManifest) -> None:
        self._manifest = manifest
        self._errors: list[AssetIssue] = []
        self._warnings: list[AssetIssue] = []
        self._diagnostics: DiagnosticsSummary = manifest.diagnostics

    def validate_all(self) -> ValidationResult:
        self.validate_manifest_structure()
        self.validate_requirements()
        self._collect_diagnostic_metadata()
        self._detect_duplicate_wheels()
        self._detect_duplicate_resources()
        self.validate_wheels()
        self.validate_resources()
        self.validate_workers()
        self._scan_worker_remote_urls()
        self.validate_service_worker()
        self._scan_sw_remote_urls()
        self._scan_html_remote_urls()
        self._check_unlocalized_urls()
        self.validate_pwa_icons()
        self.validate_outputs()
        self._validate_output_consistency()
        self._finalize_localization_stats()

        for err in self._errors:
            self._manifest.issues.append(err)
        for warn in self._warnings:
            self._manifest.issues.append(warn)

        ok = len(self._errors) == 0 and self._diagnostics.consistency.consistent
        return ValidationResult(ok=ok, errors=list(self._errors), warnings=list(self._warnings))

    def _add_error(self, message: str, location: str | None = None) -> None:
        self._errors.append(
            AssetIssue(severity=IssueSeverity.ERROR, message=message, location=location)
        )

    def _add_warning(self, message: str, location: str | None = None) -> None:
        self._warnings.append(
            AssetIssue(severity=IssueSeverity.WARNING, message=message, location=location)
        )

    def _collect_diagnostic_metadata(self) -> None:
        m = self._manifest
        self._diagnostics.localization.wheels_total = len(m.wheels)
        self._diagnostics.localization.wheels_localized = sum(
            1 for w in m.wheels.values() if w.status.localized
        )

    def _scan_remote_urls(self, content: str, location_label: str) -> list[RemoteURLRef]:
        refs: list[RemoteURLRef] = []
        seen: set[str] = set()
        if not content:
            return refs
        for pattern, scheme in _REMOTE_URL_PATTERNS:
            for match in re.finditer(pattern, content):
                url = match.group(0).rstrip(';,).')
                if url in seen:
                    continue
                seen.add(url)
                localized = False
                local_equivalent: str | None = None
                if url.startswith('file:'):
                    localized = True
                elif url.startswith('emfs:'):
                    localized = True
                elif any(host in url for host in _WELL_KNOWN_CDN_HOSTS):
                    pass
                refs.append(
                    RemoteURLRef(
                        url=url,
                        location=f"{location_label}:{match.start()}",
                        scheme=scheme,
                        localized=localized,
                        local_equivalent=local_equivalent,
                    )
                )
        return refs

    def _is_known_remote_runtime_asset(self, url: str) -> bool:
        m = self._manifest
        known_assets: list[str] = []
        try:
            from .persistence import (
                PYODIDE_URL,
                PYODIDE_JS,
                PYSCRIPT_JS,
                PYSCRIPT_CSS,
                PYSCRIPT_CSS_OVERRIDES,
            )
            known_assets = [PYODIDE_URL, PYODIDE_JS, PYSCRIPT_JS, PYSCRIPT_CSS, PYSCRIPT_CSS_OVERRIDES]
        except Exception:
            pass
        if any(known in url for known in known_assets if known):
            return True
        return False

    def _scan_worker_remote_urls(self) -> None:
        m = self._manifest
        worker = m.worker
        if worker is None or not worker.content:
            return
        refs = self._scan_remote_urls(worker.content, f"worker:{worker.worker_type.value}")
        for ref in refs:
            if self._is_known_remote_runtime_asset(ref.url):
                continue
            if not ref.localized:
                worker.remote_urls.append(ref)
                self._diagnostics.localization.total_remote_urls += 1
                self._diagnostics.localization.unlocalized_count += 1
                self._diagnostics.unlocalized_urls.append(ref)
                self._add_warning(
                    f"Worker contains non-localized remote URL: {ref.url}",
                    location=ref.location,
                )

    def _scan_sw_remote_urls(self) -> None:
        m = self._manifest
        sw = m.service_worker
        if sw is None or not sw.content:
            return
        refs = self._scan_remote_urls(sw.content, "service_worker")
        for ref in refs:
            if ref.localized or ref.url.startswith('/') or not ref.url.startswith(('http', '//')):
                continue
            sw.remote_urls.append(ref)
            self._diagnostics.localization.total_remote_urls += 1
            self._diagnostics.localization.unlocalized_count += 1
            self._diagnostics.unlocalized_urls.append(ref)
            self._add_warning(
                f"Service worker references remote URL: {ref.url}",
                location=ref.location,
            )

    def _scan_html_remote_urls(self) -> None:
        m = self._manifest
        content = m.html_content
        if not content and m.html_output and m.html_output.is_file():
            try:
                content = m.html_output.read_text(encoding='utf-8')
            except Exception:
                content = None
        if not content:
            return
        refs = self._scan_remote_urls(content, "html")
        m_local = self._diagnostics
        for ref in refs:
            if self._is_known_remote_runtime_asset(ref.url):
                m_local.localization.total_remote_urls += 1
                m_local.localization.localized_count += 1
                continue
            if not ref.localized:
                m_local.localization.total_remote_urls += 1
                m_local.localization.unlocalized_count += 1
                m_local.unlocalized_urls.append(ref)
                self._add_warning(
                    f"HTML output contains non-localized remote URL: {ref.url}",
                    location=ref.location,
                )
            else:
                m_local.localization.total_remote_urls += 1
                m_local.localization.localized_count += 1

    def _check_unlocalized_urls(self) -> None:
        m = self._manifest
        if not m.http_patch:
            return
        for wheel_name, wheel in m.wheels.items():
            if wheel.source.startswith('http') and not wheel.status.localized:
                self._add_warning(
                    f"Wheel URL not localized to emfs: {wheel_name}",
                    location=f"wheels:{wheel_name}",
                )
                self._diagnostics.missing_assets.append(wheel_name)
                self._diagnostics.localization.unlocalized_count += 1

    def _detect_duplicate_wheels(self) -> None:
        m = self._manifest
        seen_sources: dict[str, list[str]] = {}
        for name, wheel in m.wheels.items():
            key = str(wheel.local_path) if wheel.local_path else wheel.source
            seen_sources.setdefault(key, []).append(name)
        for source, names in seen_sources.items():
            if len(names) > 1:
                canonical = names[0]
                for dup in names[1:]:
                    m.wheels[dup].is_duplicate = True
                    m.wheels[dup].duplicate_of = canonical
                    self._diagnostics.duplicate_assets.append(dup)
                    self._add_warning(
                        f"Duplicate wheel: {dup} duplicates {canonical} (source={source})",
                        location=f"wheels:{dup}",
                    )

    def _detect_duplicate_resources(self) -> None:
        m = self._manifest
        seen_paths: dict[str, list[str]] = {}
        for key, resource in m.resources.items():
            seen_paths.setdefault(resource.archive_path, []).append(key)
        for archive_path, keys in seen_paths.items():
            if len(keys) > 1:
                canonical = keys[0]
                for dup in keys[1:]:
                    m.resources[dup].is_duplicate = True
                    m.resources[dup].duplicate_of = canonical
                    self._diagnostics.duplicate_assets.append(dup)
                    self._add_warning(
                        f"Duplicate archive path: {dup} -> {archive_path} (canonical {canonical})",
                        location=f"resources:{dup}",
                    )

    def _finalize_localization_stats(self) -> None:
        m = self._manifest
        diag = self._diagnostics
        diag.localization.wheels_total = len(m.wheels)
        diag.localization.wheels_localized = sum(
            1 for w in m.wheels.values() if w.status.localized
        )
        total = diag.localization.total_remote_urls
        localized = diag.localization.localized_count
        unlocalized = diag.localization.unlocalized_count
        if total == 0 and (localized or unlocalized):
            diag.localization.total_remote_urls = localized + unlocalized

    def _validate_output_consistency(self) -> None:
        m = self._manifest
        cr = self._diagnostics.consistency

        declared_outputs: set[pathlib.Path] = set()
        if m.html_output:
            declared_outputs.add(m.html_output)
        if m.resources_zip:
            declared_outputs.add(m.resources_zip)
        if m.pwa_manifest_path:
            declared_outputs.add(m.pwa_manifest_path)
        if m.worker and m.worker.output_path:
            declared_outputs.add(m.worker.output_path)
        if m.service_worker and m.service_worker.output_path:
            declared_outputs.add(m.service_worker.output_path)

        for p in declared_outputs:
            if not p.is_file():
                cr.missing_outputs.append(str(p))
                cr.consistent = False
                self._add_error(
                    f"Declared output missing from disk: {p}",
                    location=f"output:{p.name}",
                )

        if m.dest_path and m.dest_path.is_dir():
            for actual in sorted(m.dest_path.rglob('*')):
                if actual.is_dir():
                    continue
                if actual in declared_outputs:
                    continue
                expected_ext = {'.html', '.whl', '.zip', '.png', '.svg', '.ico', '.json', '.js'}
                if actual.suffix.lower() not in expected_ext:
                    continue
                relative = str(actual.relative_to(m.dest_path))
                if any(icon_name in relative for icon_name in m.pwa_icons.keys()):
                    continue
                cr.orphan_outputs.append(relative)
                self._add_warning(
                    f"Unexpected output file not tracked in manifest: {relative}",
                    location=f"output:orphan:{relative}",
                )

        if m.resources_zip and m.resources_zip.is_file():
            self._validate_zip_consistency()

    def _validate_zip_consistency(self) -> None:
        m = self._manifest
        cr = self._diagnostics.consistency
        try:
            with zipfile.ZipFile(m.resources_zip) as zf:
                names = set(zf.namelist())
        except Exception as exc:
            cr.consistent = False
            self._add_error(
                f"Resources zip is corrupted: {exc}",
                location="output:resources_zip",
            )
            return

        for resource_key, resource in m.resources.items():
            if resource.archive_path not in names:
                cr.mismatches.append(resource.archive_path)
                cr.consistent = False
                self._add_warning(
                    f"Resource declared but missing from zip: {resource.archive_path}",
                    location=f"resources:{resource_key}",
                )

        expected_wheel_names: set[str] = set()
        for wheel in m.wheels.values():
            if wheel.packed_path:
                expected_wheel_names.add(wheel.packed_path)
        for packed in expected_wheel_names:
            if packed not in names:
                cr.mismatches.append(packed)
                cr.consistent = False
                self._add_warning(
                    f"Wheel not packed into zip: {packed}",
                    location=f"output:resources_zip",
                )

    def validate_manifest_structure(self) -> None:
        m = self._manifest
        if not m.app_name:
            self._add_error("Manifest app_name is empty", location="manifest.app_name")
        if not m.app_path:
            self._add_error("Manifest app_path is empty", location="manifest.app_path")
        if not m.dest_path:
            self._add_error("Manifest dest_path is empty", location="manifest.dest_path")
        if m.runtime not in ('pyodide', 'pyscript', 'pyodide-worker', 'pyscript-worker'):
            self._add_error(
                f"Invalid runtime: {m.runtime}",
                location="manifest.runtime",
            )

    def validate_requirements(self) -> None:
        m = self._manifest
        if not m.requirements:
            self._add_warning(
                "No Python requirements collected. The app may fail at runtime.",
                location="requirements",
            )
        has_panel = any(
            'panel' in req.lower() or 'emfs:' in req for req in m.requirements
        )
        if not has_panel:
            self._add_warning(
                "Panel does not appear in requirements list.",
                location="requirements",
            )
        if m.original_requirements:
            original_set = set(m.original_requirements)
            current_set = set(m.requirements)
            dropped = original_set - current_set
            for dropped_req in sorted(dropped):
                if 'panel' in dropped_req.lower() or 'bokeh' in dropped_req.lower():
                    continue
                self._diagnostics.missing_assets.append(dropped_req)

    def validate_wheels(self) -> None:
        m = self._manifest
        for wheel_name, wheel in m.wheels.items():
            location = f"wheels:{wheel_name}"
            if wheel.is_duplicate:
                wheel.status.validated = True
                wheel.status.cache_policy = CachePolicy.PRE_CACHE
                continue
            if not wheel.local_path:
                self._add_error(f"Wheel {wheel_name} has no local_path", location=location)
                wheel.status.add_error("missing local_path", reason="local_path not set")
                self._diagnostics.missing_assets.append(wheel_name)
                continue
            if not wheel.local_path.is_file():
                self._add_error(
                    f"Wheel file does not exist: {wheel.local_path}",
                    location=location,
                )
                wheel.status.add_error(
                    f"file not found: {wheel.local_path}",
                    reason=f"wheel source missing from disk: {wheel.local_path}",
                )
                self._diagnostics.missing_assets.append(wheel_name)
                continue
            if not str(wheel.local_path).endswith('.whl'):
                self._add_warning(
                    f"Wheel file {wheel.local_path} does not have .whl extension",
                    location=location,
                )
                wheel.status.add_warning("unexpected file extension")
            if not wheel.emfs_path and wheel.status.localized:
                self._add_warning(
                    f"Wheel {wheel_name} marked localized but missing emfs_path",
                    location=location,
                )
            if wheel.status.provenance == Provenance.AUTO_DETECT and wheel.status.cache_policy == CachePolicy.UNKNOWN:
                wheel.status.cache_policy = CachePolicy.PRE_CACHE
            wheel.status.validated = True
            wheel.status.exists = True

    def validate_resources(self) -> None:
        m = self._manifest
        for resource_key, resource in m.resources.items():
            location = f"resources:{resource_key}"
            if resource.is_duplicate:
                resource.status.validated = True
                resource.status.cache_policy = CachePolicy.PRE_CACHE
                continue
            if not resource.source.is_file():
                self._add_error(
                    f"Resource file does not exist: {resource.source}",
                    location=location,
                )
                resource.status.add_error(
                    f"file not found: {resource.source}",
                    reason=f"resource source missing: {resource.source}",
                )
                self._diagnostics.missing_assets.append(resource_key)
                continue
            if not resource.archive_path:
                self._add_error(
                    f"Resource {resource_key} has no archive_path",
                    location=location,
                )
                resource.status.add_error("missing archive_path", reason="archive_path not assigned")
                continue
            if resource.status.cache_policy == CachePolicy.UNKNOWN:
                resource.status.cache_policy = CachePolicy.PRE_CACHE
            resource.status.validated = True
            resource.status.exists = True

    def validate_workers(self) -> None:
        m = self._manifest
        worker = m.worker
        if worker is None:
            return

        if worker.worker_type == WorkerType.PYODIDE:
            self._validate_pyodide_worker(worker)
        elif worker.worker_type == WorkerType.PYSCRIPT:
            self._validate_pyscript_worker(worker)

        if worker.output_path:
            if worker.output_path.is_file():
                worker.status.validated = True
                worker.status.exists = True
                worker.status.cache_policy = CachePolicy.RUNTIME_CACHE
            else:
                worker.status.add_error(
                    f"output file not found: {worker.output_path}",
                    reason=f"worker output missing: {worker.output_path}",
                )
                self._diagnostics.missing_assets.append(str(worker.output_path))

    def _validate_pyodide_worker(self, worker) -> None:
        content = worker.content
        if not content:
            self._add_error(
                "Pyodide worker content is empty",
                location="worker:pyodide",
            )
            worker.status.add_error("empty content", reason="worker.content is empty")
            return

        checks = [
            ('importScripts', 'Missing importScripts(loadPyodide) call'),
            ('loadPyodide', 'Missing loadPyodide call'),
            ('startApplication', 'Missing startApplication function'),
            ('onmessage', 'Missing onmessage handler'),
            ('sendPatch', 'Missing sendPatch function'),
            ('postMessage', 'Missing postMessage calls'),
        ]
        for token, msg in checks:
            if token not in content:
                self._add_warning(msg, location="worker:pyodide")
                worker.status.add_warning(msg)

        if 'micropip' in content and 'micropip.install' not in content:
            self._add_warning(
                "micropip loaded but install() not called",
                location="worker:pyodide",
            )

        try:
            brace_balance = 0
            in_string = False
            string_char = ''
            in_comment = False
            in_line_comment = False
            for i, ch in enumerate(content):
                if in_comment:
                    if ch == '*' and i + 1 < len(content) and content[i + 1] == '/':
                        in_comment = False
                    continue
                if in_line_comment:
                    if ch == '\n':
                        in_line_comment = False
                    continue
                if in_string:
                    if ch == string_char and (i == 0 or content[i - 1] != '\\'):
                        in_string = False
                    continue
                if ch == '/' and i + 1 < len(content):
                    if content[i + 1] == '*':
                        in_comment = True
                        continue
                    if content[i + 1] == '/':
                        in_line_comment = True
                        continue
                if ch in ('"', "'", '`'):
                    in_string = True
                    string_char = ch
                    continue
                if ch == '{':
                    brace_balance += 1
                elif ch == '}':
                    brace_balance -= 1
            if brace_balance != 0:
                self._add_warning(
                    f"Unbalanced braces in worker JS (balance={brace_balance})",
                    location="worker:pyodide",
                )
                worker.status.add_warning(f"unbalanced braces: {brace_balance}")
        except Exception:
            pass

    def _validate_pyscript_worker(self, worker) -> None:
        content = worker.content
        if not content:
            self._add_error(
                "PyScript worker content is empty",
                location="worker:pyscript",
            )
            worker.status.add_error("empty content", reason="worker.content is empty")
            return

        checks = [
            ('init_doc', 'Missing init_doc() call'),
            ('write_doc', 'Missing write_doc() call'),
            ('import asyncio', 'Missing asyncio import'),
        ]
        for token, msg in checks:
            if token not in content:
                self._add_warning(msg, location="worker:pyscript")
                worker.status.add_warning(msg)

    def validate_service_worker(self) -> None:
        m = self._manifest
        sw = m.service_worker
        if sw is None:
            if m.build_pwa:
                self._add_warning(
                    "PWA enabled but service worker not collected",
                    location="service_worker",
                )
            return

        content = sw.content
        if not content:
            self._add_error(
                "Service worker content is empty",
                location="service_worker",
            )
            sw.status.add_error("empty content", reason="service_worker.content is empty")
            return

        checks = [
            ('addEventListener', 'Missing addEventListener calls'),
            ('install', 'Missing install event listener'),
            ('activate', 'Missing activate event listener'),
            ('fetch', 'Missing fetch event listener'),
            ('caches', 'Missing caches API usage'),
            ('skipWaiting', 'Missing skipWaiting() call'),
            ('clients.claim', 'Missing clients.claim() call'),
        ]
        for token, msg in checks:
            if token not in content:
                self._add_warning(msg, location="service_worker")
                sw.status.add_warning(msg)

        cache_refs = re.findall(
            r"['\"]([^'\"]+\.(?:html|js|css|png|svg|ico|json|zip))['\"]",
            content,
        )
        if not cache_refs:
            self._add_warning(
                "No static assets referenced in service worker pre-cache list",
                location="service_worker",
            )
        else:
            dest = m.dest_path
            for ref in cache_refs:
                candidate = dest / ref
                if not candidate.is_file():
                    alt_candidate = dest / os.path.basename(ref)
                    if not alt_candidate.is_file():
                        self._add_warning(
                            f"Service worker pre-cache asset not found on disk: {ref}",
                            location=f"service_worker:cache:{ref}",
                        )
                        sw.status.add_warning(f"missing cache asset: {ref}")
                        self._diagnostics.missing_assets.append(ref)

        scope_match = re.search(r"scope:\s*['\"]([^'\"]+)['\"]", content)
        if scope_match and m.html_output:
            scope = scope_match.group(1)
            html_name = m.html_output.name
            if scope not in ('/', './', html_name, f'./{html_name}'):
                self._add_warning(
                    f"Service worker scope '{scope}' may not match HTML output '{html_name}'",
                    location="service_worker:scope",
                )

        if sw.output_path and sw.output_path.is_file():
            sw.status.validated = True
            sw.status.exists = True
            sw.status.cache_policy = CachePolicy.RUNTIME_CACHE

    def validate_pwa_icons(self) -> None:
        m = self._manifest
        if not m.build_pwa:
            return
        expected = {
            'favicon.ico',
            'icon-32x32.png',
            'icon-192x192.png',
            'icon-512x512.png',
            'icon-vector.svg',
            'apple-touch-icon.png',
        }
        for icon_name in expected:
            if icon_name not in m.pwa_icons:
                self._diagnostics.missing_assets.append(f"pwa_icon:{icon_name}")
                self._add_warning(
                    f"PWA icon missing from manifest: {icon_name}",
                    location=f"pwa_icons:{icon_name}",
                )
            else:
                status = m.pwa_icons[icon_name]
                if not getattr(status, 'exists', False):
                    self._diagnostics.missing_assets.append(f"pwa_icon:{icon_name}")
                    self._add_error(
                        f"PWA icon file not written: {icon_name}",
                        location=f"pwa_icons:{icon_name}",
                    )
                else:
                    status.cache_policy = CachePolicy.PRE_CACHE

    def validate_outputs(self) -> None:
        m = self._manifest
        if m.html_output is None:
            self._add_error("HTML output path not set", location="output:html")
            self._diagnostics.missing_assets.append("html_output")
        elif not m.html_output.is_file():
            self._add_error(
                f"HTML output file not found: {m.html_output}",
                location="output:html",
            )
            self._diagnostics.missing_assets.append(str(m.html_output))
        else:
            content = m.html_output.read_text(encoding='utf-8')
            if '<!DOCTYPE html>' not in content and '<html' not in content:
                self._add_warning(
                    "HTML output does not look like a valid HTML document",
                    location="output:html",
                )
            if '<body' not in content:
                self._add_warning(
                    "HTML output missing <body> tag",
                    location="output:html",
                )

        if m.resources_zip is not None and not m.resources_zip.is_file():
            self._add_error(
                f"Resources zip file not found: {m.resources_zip}",
                location="output:resources_zip",
            )
            self._diagnostics.missing_assets.append(str(m.resources_zip))
