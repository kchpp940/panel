from __future__ import annotations

import dataclasses
import json
import pathlib
import typing as t

from .manifest import (
    AppConversionManifest,
    IssueSeverity,
    WorkerType,
)
from .validation import ValidationResult


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


class ReportRenderer:
    def __init__(self, verbose: bool = True) -> None:
        self._verbose = verbose

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

        return AppReport(
            app_name=manifest.app_name,
            app_path=manifest.app_path,
            runtime=manifest.runtime,
            success=not errors,
            output_files=output_files,
            wheels=list(manifest.wheels.keys()),
            resources=[str(r.source) for r in manifest.resources.values()],
            errors=errors,
            warnings=warnings,
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
            return (
                f"Successfully converted {app_report.app_path} to "
                f"{app_report.runtime} target and wrote output to {outputs}."
            )
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
