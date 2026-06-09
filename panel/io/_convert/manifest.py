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


class CachePolicy(str, enum.Enum):
    PRE_CACHE = "pre-cache"
    RUNTIME_CACHE = "runtime-cache"
    NO_CACHE = "no-cache"
    UNKNOWN = "unknown"


class Provenance(str, enum.Enum):
    AUTO_DETECT = "auto-detect"
    REQUIREMENTS_FILE = "requirements-file"
    REQUIREMENTS_ARG = "requirements-arg"
    CDN_DEFAULT = "cdn-default"
    LOCAL_WHEEL = "local-wheel"
    APP_RESOURCE = "app-resource"
    PWA_TEMPLATE = "pwa-template"
    GENERATED = "generated"


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
class RemoteURLRef:
    url: str
    location: str
    scheme: str
    localized: bool = False
    local_equivalent: str | None = None

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "url": self.url,
            "location": self.location,
            "scheme": self.scheme,
            "localized": self.localized,
            "local_equivalent": self.local_equivalent,
        }


@dataclasses.dataclass
class AssetStatus:
    exists: bool = False
    validated: bool = False
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    provenance: Provenance = Provenance.AUTO_DETECT
    localized: bool = False
    cache_policy: CachePolicy = CachePolicy.UNKNOWN
    failure_reason: str | None = None
    original_url: str | None = None
    remote_refs: list[RemoteURLRef] = dataclasses.field(default_factory=list)

    def add_error(self, msg: str, reason: str | None = None) -> None:
        self.errors.append(msg)
        if reason:
            self.failure_reason = reason

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def mark_failed(self, reason: str) -> None:
        self.failure_reason = reason

    @property
    def is_ok(self) -> bool:
        return self.validated and not self.errors and not self.failure_reason


@dataclasses.dataclass
class WheelAsset:
    source: str
    local_path: pathlib.Path | None = None
    packed_path: str | None = None
    emfs_path: str | None = None
    original_source: str | None = None
    status: AssetStatus = dataclasses.field(default_factory=AssetStatus)
    is_duplicate: bool = False
    duplicate_of: str | None = None


@dataclasses.dataclass
class ResourceAsset:
    source: pathlib.Path
    archive_path: str
    status: AssetStatus = dataclasses.field(default_factory=AssetStatus)
    is_duplicate: bool = False
    duplicate_of: str | None = None


@dataclasses.dataclass
class WorkerAsset:
    worker_type: WorkerType
    content: str | None = None
    output_path: pathlib.Path | None = None
    status: AssetStatus = dataclasses.field(default_factory=AssetStatus)
    remote_urls: list[RemoteURLRef] = dataclasses.field(default_factory=list)
    referenced_assets: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class LocalizationStats:
    total_remote_urls: int = 0
    localized_count: int = 0
    unlocalized_count: int = 0
    wheels_localized: int = 0
    wheels_total: int = 0

    @property
    def localization_rate(self) -> float:
        if self.total_remote_urls == 0:
            return 1.0
        return self.localized_count / self.total_remote_urls


@dataclasses.dataclass
class ConsistencyReport:
    consistent: bool = True
    mismatches: list[str] = dataclasses.field(default_factory=list)
    orphan_outputs: list[str] = dataclasses.field(default_factory=list)
    missing_outputs: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class DiagnosticsSummary:
    localization: LocalizationStats = dataclasses.field(default_factory=LocalizationStats)
    consistency: ConsistencyReport = dataclasses.field(default_factory=ConsistencyReport)
    unlocalized_urls: list[RemoteURLRef] = dataclasses.field(default_factory=list)
    duplicate_assets: list[str] = dataclasses.field(default_factory=list)
    missing_assets: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "localization": {
                **dataclasses.asdict(self.localization),
                "localization_rate": self.localization.localization_rate,
            },
            "consistency": dataclasses.asdict(self.consistency),
            "unlocalized_urls": [u.to_dict() for u in self.unlocalized_urls],
            "duplicate_assets": list(self.duplicate_assets),
            "missing_assets": list(self.missing_assets),
        }


@dataclasses.dataclass
class AppConversionManifest:
    app_name: str
    app_path: pathlib.Path
    dest_path: pathlib.Path
    runtime: Runtimes

    requirements: list[str] = dataclasses.field(default_factory=list)
    original_requirements: list[str] = dataclasses.field(default_factory=list)
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
    diagnostics: DiagnosticsSummary = dataclasses.field(default_factory=DiagnosticsSummary)

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
            "original_requirements": list(self.original_requirements),
            "wheels": {
                k: {
                    **dataclasses.asdict(v),
                    "status": {
                        **dataclasses.asdict(v.status),
                        "provenance": v.status.provenance.value,
                        "cache_policy": v.status.cache_policy.value,
                        "remote_refs": [r.to_dict() for r in v.status.remote_refs],
                    },
                }
                for k, v in self.wheels.items()
            },
            "resources": {
                k: {
                    **dataclasses.asdict(v),
                    "status": {
                        **dataclasses.asdict(v.status),
                        "provenance": v.status.provenance.value,
                        "cache_policy": v.status.cache_policy.value,
                        "remote_refs": [r.to_dict() for r in v.status.remote_refs],
                    },
                }
                for k, v in self.resources.items()
            },
            "resources_zip": str(self.resources_zip) if self.resources_zip else None,
            "html_output": str(self.html_output) if self.html_output else None,
            "worker": dataclasses.asdict(self.worker) if self.worker else None,
            "pwa_manifest_path": str(self.pwa_manifest_path) if self.pwa_manifest_path else None,
            "service_worker": (
                dataclasses.asdict(self.service_worker) if self.service_worker else None
            ),
            "issues": [i.to_dict() for i in self.issues],
            "diagnostics": {
                **self.diagnostics.to_dict(),
            },
            "has_errors": self.has_errors,
        }
