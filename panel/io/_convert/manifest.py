from __future__ import annotations

import dataclasses
import enum
import pathlib
import typing as t

Runtimes = t.Literal['pyodide', 'pyscript', 'pyodide-worker', 'pyscript-worker']


class IssueSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class WorkerType(str, enum.Enum):
    PYODIDE = "pyodide"
    PYSCRIPT = "pyscript"
    SERVICE = "service"


@dataclasses.dataclass
class AssetIssue:
    severity: IssueSeverity
    message: str
    location: str | None = None

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "severity": self.severity.value,
            "message": self.message,
            "location": self.location,
        }


@dataclasses.dataclass
class AssetStatus:
    exists: bool = False
    validated: bool = False
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def is_ok(self) -> bool:
        return self.validated and not self.errors


@dataclasses.dataclass
class WheelAsset:
    source: str
    local_path: pathlib.Path | None = None
    packed_path: str | None = None
    emfs_path: str | None = None
    status: AssetStatus = dataclasses.field(default_factory=AssetStatus)


@dataclasses.dataclass
class ResourceAsset:
    source: pathlib.Path
    archive_path: str
    status: AssetStatus = dataclasses.field(default_factory=AssetStatus)


@dataclasses.dataclass
class WorkerAsset:
    worker_type: WorkerType
    content: str | None = None
    output_path: pathlib.Path | None = None
    status: AssetStatus = dataclasses.field(default_factory=AssetStatus)


@dataclasses.dataclass
class AppConversionManifest:
    app_name: str
    app_path: pathlib.Path
    dest_path: pathlib.Path
    runtime: Runtimes

    requirements: list[str] = dataclasses.field(default_factory=list)
    wheels: dict[str, WheelAsset] = dataclasses.field(default_factory=dict)
    resources: dict[str, ResourceAsset] = dataclasses.field(default_factory=dict)
    resources_zip: pathlib.Path | None = None

    html_output: pathlib.Path | None = None
    html_content: str | None = None
    worker: WorkerAsset | None = None

    pwa_manifest_path: pathlib.Path | None = None
    service_worker: WorkerAsset | None = None
    pwa_icons: dict[str, AssetStatus] = dataclasses.field(default_factory=dict)

    issues: list[AssetIssue] = dataclasses.field(default_factory=list)

    prerender: bool = True
    inline: bool = False
    compiled: bool = False
    http_patch: bool = True
    build_pwa: bool = False

    def add_issue(
        self,
        severity: IssueSeverity,
        message: str,
        location: str | None = None,
    ) -> None:
        self.issues.append(AssetIssue(severity=severity, message=message, location=location))

    @property
    def errors(self) -> list[AssetIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.ERROR]

    @property
    def warnings(self) -> list[AssetIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.WARNING]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == IssueSeverity.ERROR for i in self.issues)

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "app_name": self.app_name,
            "app_path": str(self.app_path),
            "dest_path": str(self.dest_path),
            "runtime": self.runtime,
            "requirements": list(self.requirements),
            "wheels": {k: dataclasses.asdict(v) for k, v in self.wheels.items()},
            "resources": {k: dataclasses.asdict(v) for k, v in self.resources.items()},
            "resources_zip": str(self.resources_zip) if self.resources_zip else None,
            "html_output": str(self.html_output) if self.html_output else None,
            "worker": dataclasses.asdict(self.worker) if self.worker else None,
            "pwa_manifest_path": str(self.pwa_manifest_path) if self.pwa_manifest_path else None,
            "service_worker": dataclasses.asdict(self.service_worker) if self.service_worker else None,
            "issues": [i.to_dict() for i in self.issues],
            "has_errors": self.has_errors,
        }
