from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import typing as t

from .manifest import (
    AppConversionManifest,
    AssetIssue,
    AssetStatus,
    IssueSeverity,
    WorkerType,
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

    def validate_all(self) -> ValidationResult:
        self.validate_manifest_structure()
        self.validate_requirements()
        self.validate_wheels()
        self.validate_resources()
        self.validate_workers()
        self.validate_service_worker()
        self.validate_pwa_icons()
        self.validate_outputs()

        for err in self._errors:
            self._manifest.issues.append(err)
        for warn in self._warnings:
            self._manifest.issues.append(warn)

        ok = len(self._errors) == 0
        return ValidationResult(ok=ok, errors=list(self._errors), warnings=list(self._warnings))

    def _add_error(self, message: str, location: str | None = None) -> None:
        self._errors.append(
            AssetIssue(severity=IssueSeverity.ERROR, message=message, location=location)
        )

    def _add_warning(self, message: str, location: str | None = None) -> None:
        self._warnings.append(
            AssetIssue(severity=IssueSeverity.WARNING, message=message, location=location)
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

    def validate_wheels(self) -> None:
        m = self._manifest
        for wheel_name, wheel in m.wheels.items():
            location = f"wheels:{wheel_name}"
            if not wheel.local_path:
                self._add_error(f"Wheel {wheel_name} has no local_path", location=location)
                wheel.status.add_error("missing local_path")
                continue
            if not wheel.local_path.is_file():
                self._add_error(
                    f"Wheel file does not exist: {wheel.local_path}",
                    location=location,
                )
                wheel.status.add_error(f"file not found: {wheel.local_path}")
                continue
            if not str(wheel.local_path).endswith('.whl'):
                self._add_warning(
                    f"Wheel file {wheel.local_path} does not have .whl extension",
                    location=location,
                )
                wheel.status.add_warning("unexpected file extension")
            if not wheel.emfs_path:
                self._add_warning(
                    f"Wheel {wheel_name} has no emfs_path set",
                    location=location,
                )
            wheel.status.validated = True

    def validate_resources(self) -> None:
        m = self._manifest
        for resource_key, resource in m.resources.items():
            location = f"resources:{resource_key}"
            if not resource.source.is_file():
                self._add_error(
                    f"Resource file does not exist: {resource.source}",
                    location=location,
                )
                resource.status.add_error(f"file not found: {resource.source}")
                continue
            if not resource.archive_path:
                self._add_error(
                    f"Resource {resource_key} has no archive_path",
                    location=location,
                )
                resource.status.add_error("missing archive_path")
                continue
            resource.status.validated = True

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
            else:
                worker.status.add_error(f"output file not found: {worker.output_path}")

    def _validate_pyodide_worker(self, worker) -> None:
        content = worker.content
        if not content:
            self._add_error(
                "Pyodide worker content is empty",
                location="worker:pyodide",
            )
            worker.status.add_error("empty content")
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
            worker.status.add_error("empty content")
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
            sw.status.add_error("empty content")
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
                self._add_warning(
                    f"PWA icon missing from manifest: {icon_name}",
                    location=f"pwa_icons:{icon_name}",
                )
            else:
                status = m.pwa_icons[icon_name]
                if not getattr(status, 'exists', False):
                    self._add_error(
                        f"PWA icon file not written: {icon_name}",
                        location=f"pwa_icons:{icon_name}",
                    )

    def validate_outputs(self) -> None:
        m = self._manifest
        if m.html_output is None:
            self._add_error("HTML output path not set", location="output:html")
        elif not m.html_output.is_file():
            self._add_error(
                f"HTML output file not found: {m.html_output}",
                location="output:html",
            )
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
