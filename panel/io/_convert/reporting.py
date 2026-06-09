from __future__ import annotations

import dataclasses
import json
import pathlib
import typing as t

from .manifest import (
    AppConversionManifest,
    CachePolicy,
    DiagnosticsSummary,
    IssueSeverity,
    Provenance,
    WorkerType,
)
from .validation import ValidationResult


@dataclasses.dataclass
class AssetDetail:
    name: str
    kind: str
    exists: bool
    validated: bool
    provenance: Provenance
    localized: bool
    cache_policy: CachePolicy
    original_url: str | None = None
    source_path: str | None = None
    output_path: str | None = None
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    failure_reason: str | None = None
    is_duplicate: bool = False
    duplicate_of: str | None = None

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "exists": self.exists,
            "validated": self.validated,
            "provenance": self.provenance.value,
            "localized": self.localized,
            "cache_policy": self.cache_policy.value,
            "original_url": self.original_url,
            "source_path": self.source_path,
            "output_path": self.output_path,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "failure_reason": self.failure_reason,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
        }


@dataclasses.dataclass
class AppReport:
    app_name: str
    app_path: pathlib.Path
    runtime: str
    success: bool
    output_files: list[str] = dataclasses.field(default_factory=list)
    wheels: list[str] = dataclasses.field(default_factory=list)
    resources: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    asset_details: list[AssetDetail] = dataclasses.field(default_factory=list)
    diagnostics: DiagnosticsSummary | None = None
    unlocalized_urls: list[dict[str, t.Any]] = dataclasses.field(default_factory=list)
    duplicate_assets: list[str] = dataclasses.field(default_factory=list)
    missing_assets: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "app_name": self.app_name,
            "app_path": str(self.app_path),
            "runtime": self.runtime,
            "success": self.success,
            "output_files": list(self.output_files),
            "wheels": list(self.wheels),
            "resources": list(self.resources),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "asset_details": [a.to_dict() for a in self.asset_details],
            "diagnostics": self.diagnostics.to_dict() if self.diagnostics else None,
            "unlocalized_urls": list(self.unlocalized_urls),
            "duplicate_assets": list(self.duplicate_assets),
            "missing_assets": list(self.missing_assets),
        }


@dataclasses.dataclass
class ConversionReport:
    total_apps: int = 0
    succeeded: int = 0
    failed: int = 0
    app_reports: list[AppReport] = dataclasses.field(default_factory=list)
    extra_outputs: list[str] = dataclasses.field(default_factory=list)
    pwa_enabled: bool = False

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "total_apps": self.total_apps,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "app_reports": [r.to_dict() for r in self.app_reports],
            "extra_outputs": list(self.extra_outputs),
            "pwa_enabled": self.pwa_enabled,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_assets_json(self, indent: int = 2) -> str:
        payload = {
            "schema_version": 1,
            "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "summary": {
                "total_apps": self.total_apps,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "pwa_enabled": self.pwa_enabled,
            },
            "apps": [self._app_assets_payload(ar) for ar in self.app_reports],
            "extra_outputs": list(self.extra_outputs),
        }
        return json.dumps(payload, indent=indent)

    @staticmethod
    def _app_assets_payload(ar: AppReport) -> dict[str, t.Any]:
        return {
            "name": ar.app_name,
            "source": str(ar.app_path),
            "runtime": ar.runtime,
            "success": ar.success,
            "outputs": list(ar.output_files),
            "assets": [a.to_dict() for a in ar.asset_details],
            "diagnostics": ar.diagnostics.to_dict() if ar.diagnostics else None,
            "issues": {
                "errors": list(ar.errors),
                "warnings": list(ar.warnings),
                "unlocalized_urls": list(ar.unlocalized_urls),
                "duplicate_assets": list(ar.duplicate_assets),
                "missing_assets": list(ar.missing_assets),
            },
        }

    def to_assets_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# Panel Conversion Assets Report")
        lines.append("")
        lines.append(f"- **Total apps:** {self.total_apps}")
        lines.append(f"- **Succeeded:** {self.succeeded}")
        lines.append(f"- **Failed:** {self.failed}")
        lines.append(f"- **PWA enabled:** {self.pwa_enabled}")
        lines.append("")

        for ar in self.app_reports:
            status = "✅ OK" if ar.success else "❌ FAILED"
            lines.append(f"## {ar.app_name} — {status}")
            lines.append("")
            lines.append(f"- **Runtime:** `{ar.runtime}`")
            lines.append(f"- **Source:** `{ar.app_path}`")
            lines.append("")

            if ar.output_files:
                lines.append("### Output files")
                lines.append("")
                for out in ar.output_files:
                    lines.append(f"- `{out}`")
                lines.append("")

            lines.append("### Asset inventory")
            lines.append("")
            lines.append(
                "| # | Kind | Name | Provenance | Localized | Cache | Status | Failure reason |"
            )
            lines.append(
                "|---|------|------|------------|-----------|-------|--------|----------------|"
            )
            for idx, ad in enumerate(ar.asset_details, 1):
                status_parts = []
                if ad.validated:
                    status_parts.append("validated")
                if not ad.exists:
                    status_parts.append("MISSING")
                if ad.is_duplicate:
                    status_parts.append(f"dup of {ad.duplicate_of}")
                if ad.errors:
                    status_parts.append(f"{len(ad.errors)} err")
                if ad.warnings:
                    status_parts.append(f"{len(ad.warnings)} warn")
                status_text = ", ".join(status_parts) if status_parts else "ok"
                lines.append(
                    f"| {idx} | {ad.kind} | `{ad.name}` | {ad.provenance.value} | "
                    f"{'yes' if ad.localized else 'no'} | {ad.cache_policy.value} | "
                    f"{status_text} | {ad.failure_reason or ''} |"
                )
            lines.append("")

            if ar.diagnostics:
                d = ar.diagnostics
                lines.append("### Diagnostics")
                lines.append("")
                lines.append("#### Localization")
                lines.append("")
                rate = d.localization.localization_rate
                lines.append(
                    f"- Total remote URLs: **{d.localization.total_remote_urls}**"
                )
                lines.append(
                    f"- Localized: **{d.localization.localized_count}**"
                )
                lines.append(
                    f"- Unlocalized: **{d.localization.unlocalized_count}**"
                )
                lines.append(
                    f"- Wheels localized: {d.localization.wheels_localized}/{d.localization.wheels_total}"
                )
                lines.append(
                    f"- Localization rate: **{rate * 100:.1f}%**"
                )
                lines.append("")

                lines.append("#### Consistency")
                lines.append("")
                lines.append(
                    f"- Consistent: **{'yes' if d.consistency.consistent else 'NO'}**"
                )
                if d.consistency.missing_outputs:
                    lines.append("- Missing outputs:")
                    for mo in d.consistency.missing_outputs:
                        lines.append(f"  - `{mo}`")
                if d.consistency.orphan_outputs:
                    lines.append("- Orphan outputs (on disk but unclaimed):")
                    for oo in d.consistency.orphan_outputs:
                        lines.append(f"  - `{oo}`")
                if d.consistency.mismatches:
                    lines.append("- Manifest vs zip mismatches:")
                    for mm in d.consistency.mismatches:
                        lines.append(f"  - `{mm}`")
                lines.append("")

                if d.unlocalized_urls:
                    lines.append("#### Unlocalized URLs")
                    lines.append("")
                    for u in d.unlocalized_urls:
                        lines.append(
                            f"- `{u.url}` ({u.scheme}) — at `{u.location}`"
                        )
                    lines.append("")

                if d.duplicate_assets:
                    lines.append("#### Duplicate assets")
                    lines.append("")
                    for da in d.duplicate_assets:
                        lines.append(f"- `{da}`")
                    lines.append("")

                if d.missing_assets:
                    lines.append("#### Missing assets")
                    lines.append("")
                    for ma in d.missing_assets:
                        lines.append(f"- `{ma}`")
                    lines.append("")

            if ar.errors:
                lines.append("### Errors")
                lines.append("")
                for err in ar.errors:
                    lines.append(f"- ❌ {err}")
                lines.append("")

            if ar.warnings:
                lines.append("### Warnings")
                lines.append("")
                for warn in ar.warnings:
                    lines.append(f"- ⚠️ {warn}")
                lines.append("")

        if self.extra_outputs:
            lines.append("## Extra outputs")
            lines.append("")
            for out in self.extra_outputs:
                lines.append(f"- `{out}`")
            lines.append("")

        return "\n".join(lines)


class ReportRenderer:
    def __init__(self, verbose: bool = True) -> None:
        self._verbose = verbose

    def _collect_asset_details(
        self, manifest: AppConversionManifest
    ) -> list[AssetDetail]:
        details: list[AssetDetail] = []

        for wheel_name, wheel in manifest.wheels.items():
            details.append(
                AssetDetail(
                    name=wheel_name,
                    kind="wheel",
                    exists=wheel.status.exists,
                    validated=wheel.status.validated,
                    provenance=wheel.status.provenance,
                    localized=wheel.status.localized,
                    cache_policy=wheel.status.cache_policy,
                    original_url=wheel.status.original_url or wheel.original_source,
                    source_path=str(wheel.local_path) if wheel.local_path else None,
                    output_path=wheel.packed_path,
                    errors=list(wheel.status.errors),
                    warnings=list(wheel.status.warnings),
                    failure_reason=wheel.status.failure_reason,
                    is_duplicate=wheel.is_duplicate,
                    duplicate_of=wheel.duplicate_of,
                )
            )

        for resource_key, resource in manifest.resources.items():
            details.append(
                AssetDetail(
                    name=resource_key,
                    kind="resource",
                    exists=resource.status.exists,
                    validated=resource.status.validated,
                    provenance=resource.status.provenance,
                    localized=resource.status.localized,
                    cache_policy=resource.status.cache_policy,
                    original_url=resource.status.original_url,
                    source_path=str(resource.source),
                    output_path=resource.archive_path,
                    errors=list(resource.status.errors),
                    warnings=list(resource.status.warnings),
                    failure_reason=resource.status.failure_reason,
                    is_duplicate=resource.is_duplicate,
                    duplicate_of=resource.duplicate_of,
                )
            )

        if manifest.worker is not None:
            w = manifest.worker
            details.append(
                AssetDetail(
                    name=f"worker:{w.worker_type.value}",
                    kind="worker",
                    exists=w.status.exists,
                    validated=w.status.validated,
                    provenance=w.status.provenance,
                    localized=w.status.localized,
                    cache_policy=w.status.cache_policy,
                    original_url=w.status.original_url,
                    source_path=None,
                    output_path=str(w.output_path) if w.output_path else None,
                    errors=list(w.status.errors),
                    warnings=list(w.status.warnings),
                    failure_reason=w.status.failure_reason,
                )
            )

        if manifest.service_worker is not None:
            sw = manifest.service_worker
            details.append(
                AssetDetail(
                    name="service-worker",
                    kind="service-worker",
                    exists=sw.status.exists,
                    validated=sw.status.validated,
                    provenance=sw.status.provenance,
                    localized=sw.status.localized,
                    cache_policy=sw.status.cache_policy,
                    original_url=sw.status.original_url,
                    source_path=None,
                    output_path=str(sw.output_path) if sw.output_path else None,
                    errors=list(sw.status.errors),
                    warnings=list(sw.status.warnings),
                    failure_reason=sw.status.failure_reason,
                )
            )

        for icon_name, status in manifest.pwa_icons.items():
            details.append(
                AssetDetail(
                    name=f"pwa-icon:{icon_name}",
                    kind="pwa-icon",
                    exists=status.exists,
                    validated=status.validated,
                    provenance=status.provenance,
                    localized=status.localized,
                    cache_policy=status.cache_policy,
                    original_url=status.original_url,
                    source_path=None,
                    output_path=f"images/{icon_name}",
                    errors=list(status.errors),
                    warnings=list(status.warnings),
                    failure_reason=status.failure_reason,
                )
            )

        if manifest.html_output is not None:
            details.append(
                AssetDetail(
                    name=manifest.html_output.name,
                    kind="html",
                    exists=manifest.html_output.is_file(),
                    validated=True,
                    provenance=Provenance.GENERATED,
                    localized=manifest.inline,
                    cache_policy=CachePolicy.PRE_CACHE,
                    source_path=str(manifest.app_path),
                    output_path=str(manifest.html_output),
                )
            )

        if manifest.resources_zip is not None:
            details.append(
                AssetDetail(
                    name=manifest.resources_zip.name,
                    kind="resources-zip",
                    exists=manifest.resources_zip.is_file(),
                    validated=manifest.resources_zip.is_file(),
                    provenance=Provenance.GENERATED,
                    localized=True,
                    cache_policy=CachePolicy.PRE_CACHE,
                    output_path=str(manifest.resources_zip),
                )
            )

        return details

    def build_app_report(
        self,
        manifest: AppConversionManifest,
        validation: ValidationResult | None = None,
    ) -> AppReport:
        errors: list[str] = []
        warnings: list[str] = []

        if validation is not None:
            errors.extend(e.message for e in validation.errors)
            warnings.extend(w.message for w in validation.warnings)

        for issue in manifest.issues:
            msg = issue.message
            if issue.location:
                msg = f"[{issue.location}] {msg}"
            if issue.severity == IssueSeverity.ERROR:
                errors.append(msg)
            elif issue.severity == IssueSeverity.WARNING:
                warnings.append(msg)

        output_files: list[str] = []
        if manifest.html_output:
            output_files.append(str(manifest.html_output))
        if manifest.worker and manifest.worker.output_path:
            output_files.append(str(manifest.worker.output_path))
        if manifest.resources_zip:
            output_files.append(str(manifest.resources_zip))
        if manifest.pwa_manifest_path:
            output_files.append(str(manifest.pwa_manifest_path))
        if manifest.service_worker and manifest.service_worker.output_path:
            output_files.append(str(manifest.service_worker.output_path))

        asset_details = self._collect_asset_details(manifest)

        unlocalized_urls = [u.to_dict() for u in manifest.diagnostics.unlocalized_urls]

        return AppReport(
            app_name=manifest.app_name,
            app_path=manifest.app_path,
            runtime=manifest.runtime,
            success=not errors and manifest.diagnostics.consistency.consistent,
            output_files=output_files,
            wheels=list(manifest.wheels.keys()),
            resources=[str(r.source) for r in manifest.resources.values()],
            errors=errors,
            warnings=warnings,
            asset_details=asset_details,
            diagnostics=manifest.diagnostics,
            unlocalized_urls=unlocalized_urls,
            duplicate_assets=list(manifest.diagnostics.duplicate_assets),
            missing_assets=list(manifest.diagnostics.missing_assets),
        )

    def render_console(self, report: ConversionReport) -> str:
        lines: list[str] = []

        lines.append("")
        lines.append("=" * 60)
        lines.append("Panel Conversion Report")
        lines.append("=" * 60)
        lines.append(f"  Total apps:      {report.total_apps}")
        lines.append(f"  Succeeded:       {report.succeeded}")
        lines.append(f"  Failed:          {report.failed}")
        if report.pwa_enabled:
            lines.append(f"  PWA:             enabled")
        lines.append("")

        for app_report in report.app_reports:
            status = "OK" if app_report.success else "FAILED"
            lines.append(f"  [{status}] {app_report.app_name} ({app_report.runtime})")
            lines.append(f"       source:  {app_report.app_path}")
            for out in app_report.output_files:
                lines.append(f"       output:  {out}")
            if app_report.wheels:
                lines.append(f"       wheels:  {', '.join(app_report.wheels)}")
            if app_report.resources:
                lines.append(f"       data:    {len(app_report.resources)} resource(s)")
            if app_report.diagnostics:
                d = app_report.diagnostics
                rate = d.localization.localization_rate * 100
                lines.append(
                    f"       locale:  {d.localization.localized_count}/{d.localization.total_remote_urls} URLs localized ({rate:.0f}%)"
                )
                if d.localization.unlocalized_count:
                    lines.append(
                        f"                ⚠ {d.localization.unlocalized_count} unlocalized URL(s)"
                    )
                if not d.consistency.consistent:
                    lines.append("                ✗ manifest/output consistency FAILED")
                if d.duplicate_assets:
                    lines.append(
                        f"                ⚠ {len(d.duplicate_assets)} duplicate asset(s)"
                    )
                if d.missing_assets:
                    lines.append(
                        f"                ✗ {len(d.missing_assets)} missing asset(s)"
                    )
            for warning in app_report.warnings:
                lines.append(f"       ! warn:  {warning}")
            for error in app_report.errors:
                lines.append(f"       X error: {error}")
            lines.append("")

        if report.extra_outputs:
            lines.append("  Additional outputs:")
            for out in report.extra_outputs:
                lines.append(f"    - {out}")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)

    def print_console(self, report: ConversionReport) -> None:
        if not self._verbose:
            return
        print(self.render_console(report))

    def render_app_summary(self, app_report: AppReport) -> str:
        if app_report.success:
            outputs = ", ".join(
                pathlib.Path(f).name for f in app_report.output_files
            )
            base = (
                f"Successfully converted {app_report.app_path} to "
                f"{app_report.runtime} target and wrote output to {outputs}."
            )
            if app_report.diagnostics:
                rate = app_report.diagnostics.localization.localization_rate * 100
                base += f" (URL localization: {rate:.0f}%)"
            return base
        else:
            errors = "; ".join(app_report.errors[:3])
            return (
                f"Failed to convert {app_report.app_path} to "
                f"{app_report.runtime} target: {errors}"
            )

    def print_app_summary(self, app_report: AppReport) -> None:
        if not self._verbose:
            return
        print(self.render_app_summary(app_report))

    def write_assets_report(
        self,
        report: ConversionReport,
        destination: str | pathlib.Path,
        *,
        format: t.Literal['json', 'md', 'both'] = 'both',
    ) -> list[pathlib.Path]:
        dest = pathlib.Path(destination)
        written: list[pathlib.Path] = []
        if format in ('json', 'both'):
            json_path = dest / 'assets-report.json'
            json_path.write_text(report.to_assets_json(), encoding='utf-8')
            written.append(json_path)
        if format in ('md', 'both'):
            md_path = dest / 'assets-report.md'
            md_path.write_text(report.to_assets_markdown(), encoding='utf-8')
            written.append(md_path)
        return written
