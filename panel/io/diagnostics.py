from __future__ import annotations

import json
import logging
import os
import sys
import typing as t
from dataclasses import dataclass, field, asdict
from enum import Enum

if t.TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class StartupMode(str, Enum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    AUTO = "auto"


def _severity_ge(a: DiagnosticSeverity, b: DiagnosticSeverity) -> bool:
    order = [
        DiagnosticSeverity.INFO,
        DiagnosticSeverity.WARNING,
        DiagnosticSeverity.ERROR,
        DiagnosticSeverity.FATAL,
    ]
    return order.index(a) >= order.index(b)


def _downgrade_for_dev(
    severity: DiagnosticSeverity,
    mode: StartupMode,
    threshold: DiagnosticSeverity = DiagnosticSeverity.ERROR,
    steps: int = 1,
) -> DiagnosticSeverity:
    if mode != StartupMode.DEVELOPMENT:
        return severity
    if _severity_ge(severity, threshold):
        order = [
            DiagnosticSeverity.INFO,
            DiagnosticSeverity.WARNING,
            DiagnosticSeverity.ERROR,
            DiagnosticSeverity.FATAL,
        ]
        idx = order.index(severity)
        return order[max(0, idx - steps)]
    return severity


@dataclass
class DiagnosticIssue:
    severity: DiagnosticSeverity
    code: str
    message: str
    details: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "suggestion": self.suggestion,
        }


@dataclass
class ServiceDiagnostic:
    name: str
    enabled: bool
    status: DiagnosticSeverity
    issues: list[DiagnosticIssue] = field(default_factory=list)
    config: dict[str, t.Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "status": self.status.value,
            "issues": [issue.to_dict() for issue in self.issues],
            "config": self.config,
        }


@dataclass
class StartupDiagnosticResult:
    timestamp: str
    startup_mode: StartupMode
    overall_status: DiagnosticSeverity
    services: dict[str, ServiceDiagnostic] = field(default_factory=dict)
    issues: list[DiagnosticIssue] = field(default_factory=list)

    @property
    def has_fatal(self) -> bool:
        return any(
            issue.severity == DiagnosticSeverity.FATAL
            for issue in self.issues
        ) or any(
            service.status == DiagnosticSeverity.FATAL
            for service in self.services.values()
        )

    @property
    def has_errors(self) -> bool:
        return any(
            issue.severity in (DiagnosticSeverity.ERROR, DiagnosticSeverity.FATAL)
            for issue in self.issues
        ) or any(
            service.status in (DiagnosticSeverity.ERROR, DiagnosticSeverity.FATAL)
            for service in self.services.values()
        )

    @property
    def has_warnings(self) -> bool:
        return any(
            issue.severity == DiagnosticSeverity.WARNING
            for issue in self.issues
        ) or any(
            service.status == DiagnosticSeverity.WARNING
            for service in self.services.values()
        )

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "timestamp": self.timestamp,
            "startup_mode": self.startup_mode.value,
            "overall_status": self.overall_status.value,
            "has_fatal": self.has_fatal,
            "has_errors": self.has_errors,
            "has_warnings": self.has_warnings,
            "services": {name: svc.to_dict() for name, svc in self.services.items()},
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def format_report(self) -> str:
        lines = [
            "=" * 70,
            "Panel Server Startup Diagnostic Report",
            "=" * 70,
            f"Timestamp: {self.timestamp}",
            f"Startup Mode: {self.startup_mode.value.upper()}",
            f"Overall Status: {self.overall_status.value.upper()}",
            "",
        ]

        if self.issues:
            lines.append("Global Issues:")
            lines.append("-" * 70)
            for issue in self.issues:
                prefix = f"[{issue.severity.value.upper()}]"
                lines.append(f"{prefix} ({issue.code}) {issue.message}")
                if issue.details:
                    lines.append(f"    Details: {issue.details}")
                if issue.suggestion:
                    lines.append(f"    Suggestion: {issue.suggestion}")
            lines.append("")

        for service_name, service in self.services.items():
            lines.append(f"Service: {service_name}")
            lines.append(f"  Enabled: {service.enabled}")
            lines.append(f"  Status: {service.status.value.upper()}")
            if service.config:
                lines.append("  Configuration:")
                for key, value in service.config.items():
                    lines.append(f"    {key}: {_format_config_value(value)}")
            if service.issues:
                lines.append("  Issues:")
                for issue in service.issues:
                    prefix = f"    [{issue.severity.value.upper()}]"
                    lines.append(f"{prefix} ({issue.code}) {issue.message}")
                    if issue.details:
                        lines.append(f"        Details: {issue.details}")
                    if issue.suggestion:
                        lines.append(f"        Suggestion: {issue.suggestion}")
            lines.append("")

        if self.has_fatal or self.has_errors:
            lines.append("-" * 70)
            lines.append("ERRORS DETECTED - Server startup has been blocked.")
            lines.append("Please fix the above issues before starting the server.")
            if self.startup_mode == StartupMode.DEVELOPMENT:
                lines.append(
                    "Note: Running in DEVELOPMENT mode. Some security checks are "
                    "downgraded to warnings."
                )
        elif self.has_warnings:
            lines.append("-" * 70)
            lines.append(
                "WARNINGS DETECTED - Server will start but some features may not "
                "work correctly."
            )
            if self.startup_mode == StartupMode.DEVELOPMENT:
                lines.append(
                    "Note: Running in DEVELOPMENT mode. Some issues are shown as "
                    "warnings that would be errors in PRODUCTION mode."
                )

        lines.append("=" * 70)
        return "\n".join(lines)


def _format_config_value(value: t.Any) -> str:
    if isinstance(value, str) and len(value) > 50:
        return value[:47] + "..."
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}({len(value)} items)"
    if isinstance(value, dict):
        return f"dict({len(value)} keys)"
    return repr(value)


def _get_timestamp() -> str:
    import datetime as dt
    return dt.datetime.now().isoformat()


def _highest_severity(severities: list[DiagnosticSeverity]) -> DiagnosticSeverity:
    order = [
        DiagnosticSeverity.INFO,
        DiagnosticSeverity.WARNING,
        DiagnosticSeverity.ERROR,
        DiagnosticSeverity.FATAL,
    ]
    if not severities:
        return DiagnosticSeverity.INFO
    return max(severities, key=lambda s: order.index(s))


@dataclass
class StartupConfig:
    mode: StartupMode = StartupMode.AUTO

    websocket_origin: str | list[str] | None = None
    address: str | None = None
    port: int | None = None

    static_dirs: Mapping[str, str] | None = None

    autoreload: bool | None = None
    dev: bool | None = None

    admin: bool = False
    admin_endpoint: str | None = None

    session_history: int | None = None
    check_unused_sessions: int | None = None

    notifications: bool | None = None
    browser_info: bool | None = None

    extra_checks: list[t.Callable[[], ServiceDiagnostic]] = field(default_factory=list)

    @classmethod
    def resolve(
        cls,
        *,
        websocket_origin: str | list[str] | None = None,
        address: str | None = None,
        port: int | None = None,
        static_dirs: Mapping[str, str] | None = None,
        autoreload: bool | None = None,
        dev: bool | None = None,
        admin: bool = False,
        admin_endpoint: str | None = None,
        session_history: int | None = None,
        check_unused_sessions: int | None = None,
        notifications: bool | None = None,
        browser_info: bool | None = None,
        mode: StartupMode | str = StartupMode.AUTO,
        extra_checks: list[t.Callable[[], ServiceDiagnostic]] | None = None,
    ) -> "StartupConfig":
        from ..config import config

        if isinstance(mode, str):
            mode = StartupMode(mode)

        resolved = cls(
            mode=mode,
            websocket_origin=websocket_origin,
            address=address,
            port=port,
            static_dirs=dict(static_dirs) if static_dirs else None,
            autoreload=autoreload if autoreload is not None else bool(config.autoreload),
            dev=bool(dev),
            admin=bool(admin or config._admin),
            admin_endpoint=admin_endpoint or config.admin_endpoint,
            session_history=(
                session_history
                if session_history is not None
                else config.session_history
            ),
            check_unused_sessions=check_unused_sessions,
            notifications=(
                notifications if notifications is not None else bool(config.notifications)
            ),
            browser_info=(
                browser_info if browser_info is not None else bool(config.browser_info)
            ),
            extra_checks=extra_checks or [],
        )
        return resolved

    @property
    def resolved_websocket_origin_list(self) -> list[str]:
        if not self.websocket_origin:
            return []
        if isinstance(self.websocket_origin, list):
            return list(self.websocket_origin)
        return [self.websocket_origin]

    @property
    def normalized_static_dirs(self) -> dict[str, str]:
        from ..util import fullpath

        if not self.static_dirs:
            return {}
        normalized: dict[str, str] = {}
        for slug, path in self.static_dirs.items():
            if not slug.startswith("/"):
                slug = "/" + slug
            normalized[slug] = fullpath(path)
        return normalized

    @property
    def resolved_admin_endpoint(self) -> str:
        endpoint = self.admin_endpoint or "/admin"
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return endpoint

    @property
    def effective_autoreload(self) -> bool:
        return bool(self.autoreload)

    def detect_mode(self) -> StartupMode:
        if self.mode != StartupMode.AUTO:
            return self.mode

        if self.dev or self.autoreload:
            return StartupMode.DEVELOPMENT

        if os.environ.get("PANEL_ENV"):
            env_val = os.environ["PANEL_ENV"].lower()
            if env_val in ("dev", "development"):
                return StartupMode.DEVELOPMENT
            if env_val in ("prod", "production"):
                return StartupMode.PRODUCTION

        if os.environ.get("PYTHON_ENV"):
            env_val = os.environ["PYTHON_ENV"].lower()
            if env_val in ("dev", "development"):
                return StartupMode.DEVELOPMENT
            if env_val in ("prod", "production"):
                return StartupMode.PRODUCTION

        if self.address and self.address not in ("localhost", "127.0.0.1", None):
            return StartupMode.PRODUCTION

        return StartupMode.DEVELOPMENT

    def apply_to_config(self) -> None:
        from ..config import config

        if self.admin:
            config._admin = True
        if self.admin_endpoint:
            config._admin_endpoint = self.admin_endpoint
        if self.session_history is not None:
            config.session_history = self.session_history
        if self.autoreload is not None:
            config.autoreload = self.autoreload
        if self.notifications is not None:
            config.notifications = self.notifications
        if self.browser_info is not None:
            config.browser_info = self.browser_info



DiagnosticContext = StartupConfig


def diagnose_websocket_origin(config: StartupConfig) -> ServiceDiagnostic:
    issues: list[DiagnosticIssue] = []
    service_config: dict[str, t.Any] = {}
    mode = config.detect_mode()

    origins: list[str] = []
    if config.websocket_origin:
        origins = (
            config.websocket_origin
            if isinstance(config.websocket_origin, list)
            else [config.websocket_origin]
        )
        service_config["explicit_origins"] = origins

    service_config["allow_websocket_origin"] = origins or ["localhost"]
    service_config["address"] = config.address or "localhost"
    if config.port:
        service_config["port"] = config.port

    if not origins:
        severity = _downgrade_for_dev(
            DiagnosticSeverity.WARNING,
            mode,
            threshold=DiagnosticSeverity.INFO,
            steps=1,
        )
        issues.append(DiagnosticIssue(
            severity=severity,
            code="WS_ORIGIN_DEFAULT",
            message="No explicit WebSocket origin configured, using default 'localhost'.",
            details=(
                "This is only safe for local development. In production, you must "
                "specify allowed origins."
            ),
            suggestion=(
                "Use the --allow-websocket-origin CLI argument or set "
                "PANEL_ALLOW_WEBSOCKET_ORIGIN environment variable with the "
                "allowed hostnames."
            ),
        ))
    else:
        for origin in origins:
            if "*" in origin:
                issues.append(DiagnosticIssue(
                    severity=DiagnosticSeverity.FATAL,
                    code="WS_ORIGIN_WILDCARD",
                    message=f"Wildcard '*' detected in WebSocket origin: {origin!r}.",
                    details=(
                        "Allowing any origin via wildcard is a critical security "
                        "vulnerability that enables CSRF attacks."
                    ),
                    suggestion=(
                        "Replace wildcard origins with explicit, trusted hostnames. "
                        "E.g., 'example.com' instead of '*'."
                    ),
                ))
            if origin.startswith(("http://", "https://")):
                issues.append(DiagnosticIssue(
                    severity=DiagnosticSeverity.WARNING,
                    code="WS_ORIGIN_SCHEME",
                    message=f"WebSocket origin {origin!r} includes URL scheme.",
                    details=(
                        "Bokeh/Panel WebSocket origin matching only considers the "
                        "hostname and port, not the scheme."
                    ),
                    suggestion=(
                        "Specify origins as 'host:port' without scheme, "
                        "e.g., 'example.com:443'."
                    ),
                ))

    status = _highest_severity([i.severity for i in issues]) if issues else DiagnosticSeverity.INFO
    return ServiceDiagnostic(
        name="websocket_origin",
        enabled=True,
        status=status,
        issues=issues,
        config=service_config,
    )


def diagnose_static_dirs(config: StartupConfig) -> ServiceDiagnostic:
    from ..util import fullpath

    issues: list[DiagnosticIssue] = []
    service_config: dict[str, t.Any] = {}
    enabled = bool(config.static_dirs)
    mode = config.detect_mode()

    if not enabled:
        return ServiceDiagnostic(
            name="static_dirs",
            enabled=False,
            status=DiagnosticSeverity.INFO,
            issues=[],
            config={},
        )

    static_dirs = config.static_dirs or {}

    for slug, path in static_dirs.items():
        normalized_slug = slug if slug.startswith("/") else "/" + slug
        service_config[normalized_slug] = path

        if normalized_slug == "/static":
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.FATAL,
                code="STATIC_DIR_RESERVED",
                message=f"Static file route {normalized_slug!r} is reserved for internal use.",
                details=(
                    "The '/static' route is used by Panel/Bokeh to serve "
                    "internal resources."
                ),
                suggestion=(
                    "Use a different route slug, e.g., '/assets' instead of "
                    "'/static'."
                ),
            ))

        resolved_path = fullpath(path)
        if not os.path.exists(resolved_path):
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.FATAL,
                code="STATIC_DIR_NOT_FOUND",
                message=(
                    f"Static directory not found: {resolved_path!r} "
                    f"(mapped from {path!r})."
                ),
                details=(
                    "The specified static file directory does not exist on "
                    "the filesystem."
                ),
                suggestion=(
                    "Ensure the directory exists before starting the server, "
                    "or correct the path in the static_dirs configuration."
                ),
            ))
        elif not os.path.isdir(resolved_path):
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.FATAL,
                code="STATIC_DIR_NOT_DIRECTORY",
                message=f"Static path is not a directory: {resolved_path!r}.",
                details="Static file serving requires a directory, not a file.",
                suggestion="Provide a path to an existing directory.",
            ))
        else:
            try:
                if not os.access(resolved_path, os.R_OK):
                    issues.append(DiagnosticIssue(
                        severity=_downgrade_for_dev(
                            DiagnosticSeverity.ERROR, mode
                        ),
                        code="STATIC_DIR_NO_READ",
                        message=f"Static directory is not readable: {resolved_path!r}.",
                        details=(
                            "The server process does not have read permission "
                            "for the static directory."
                        ),
                        suggestion=(
                            "Adjust the directory permissions to allow the "
                            "server process to read its contents."
                        ),
                    ))
            except Exception:
                pass

        normalized_no_slash = normalized_slug.rstrip("/")
        if normalized_slug != normalized_no_slash and normalized_no_slash != "/":
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.WARNING,
                code="STATIC_DIR_TRAILING_SLASH",
                message=f"Static route slug {normalized_slug!r} has a trailing slash.",
                details=(
                    "Trailing slashes in route slugs can cause unexpected "
                    "routing behavior."
                ),
                suggestion="Remove the trailing slash from the route slug.",
            ))

    slugs = [(s if s.startswith("/") else "/" + s) for s in static_dirs.keys()]
    if len(slugs) != len(set(slugs)):
        issues.append(DiagnosticIssue(
            severity=_downgrade_for_dev(DiagnosticSeverity.ERROR, mode),
            code="STATIC_DIR_DUPLICATE",
            message="Duplicate static directory routes detected.",
            details=f"Routes after normalization: {slugs}",
            suggestion="Ensure each static directory has a unique route slug.",
        ))

    status = _highest_severity([i.severity for i in issues])
    return ServiceDiagnostic(
        name="static_dirs",
        enabled=enabled,
        status=status,
        issues=issues,
        config=service_config,
    )


def diagnose_autoreload(config: StartupConfig) -> ServiceDiagnostic:
    issues: list[DiagnosticIssue] = []
    service_config: dict[str, t.Any] = {}
    mode = config.detect_mode()

    enabled = bool(config.autoreload)
    dev_mode = bool(config.dev)

    service_config["autoreload"] = enabled
    service_config["dev_mode"] = dev_mode
    service_config["env_var"] = os.environ.get("PANEL_AUTORELOAD")

    if enabled:
        if mode == StartupMode.PRODUCTION:
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.WARNING,
                code="AUTORELOAD_PRODUCTION",
                message="Autoreload is enabled in production mode.",
                details=(
                    "Autoreload watches for file changes and restarts the "
                    "server. This has significant performance overhead and "
                    "is intended for development only."
                ),
                suggestion=(
                    "Disable autoreload in production environments by "
                    "setting PANEL_AUTORELOAD=False or not using "
                    "--autoreload/--dev."
                ),
            ))
        else:
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.INFO,
                code="AUTORELOAD_DEV",
                message="Autoreload is enabled (development mode).",
            ))

        if "PANEL_AUTORELOAD" in os.environ:
            env_value = os.environ["PANEL_AUTORELOAD"]
            if env_value.lower() in ("true", "1") and not dev_mode and config.autoreload is None:
                issues.append(DiagnosticIssue(
                    severity=DiagnosticSeverity.INFO,
                    code="AUTORELOAD_ENV_ENABLED",
                    message=(
                        "Autoreload enabled via PANEL_AUTORELOAD environment "
                        "variable."
                    ),
                ))

        watcher_module = None
        try:
            from . import reload as _reload_module
            watcher_module = _reload_module
        except Exception:
            pass

        service_config["watchdog_available"] = watcher_module is not None
        if not watcher_module:
            issues.append(DiagnosticIssue(
                severity=_downgrade_for_dev(DiagnosticSeverity.ERROR, mode),
                code="AUTORELOAD_WATCHER_MISSING",
                message="File watcher module is not available.",
                details=(
                    "The autoreload feature requires the internal file "
                    "watching infrastructure to be available."
                ),
                suggestion=(
                    "Ensure the panel installation is complete and not "
                    "corrupted."
                ),
            ))

    status = _highest_severity([i.severity for i in issues]) if issues else DiagnosticSeverity.INFO
    return ServiceDiagnostic(
        name="autoreload",
        enabled=enabled,
        status=status,
        issues=issues,
        config=service_config,
    )


def diagnose_admin_endpoint(config: StartupConfig) -> ServiceDiagnostic:
    from ..config import config as _global_config

    issues: list[DiagnosticIssue] = []
    service_config: dict[str, t.Any] = {}
    mode = config.detect_mode()

    enabled = bool(config.admin)
    endpoint = config.admin_endpoint or _global_config.admin_endpoint or "/admin"

    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    service_config["enabled"] = enabled
    service_config["endpoint"] = endpoint
    service_config["log_level"] = _global_config.admin_log_level

    if enabled:
        if endpoint == "/":
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.FATAL,
                code="ADMIN_ENDPOINT_ROOT",
                message="Admin endpoint cannot be set to the root path '/'.",
                details="The root path is used by the Panel application index.",
                suggestion="Use a distinct path for the admin endpoint, e.g., '/admin'.",
            ))

        if _global_config.admin_log_level in ("DEBUG",):
            issues.append(DiagnosticIssue(
                severity=_downgrade_for_dev(
                    DiagnosticSeverity.WARNING, mode, DiagnosticSeverity.INFO
                ),
                code="ADMIN_LOG_LEVEL_DEBUG",
                message=(
                    f"Admin panel log level is set to "
                    f"{_global_config.admin_log_level!r}."
                ),
                details=(
                    "DEBUG level logging may expose sensitive information "
                    "in logs."
                ),
                suggestion=(
                    "Consider using INFO or higher log levels in "
                    "production environments."
                ),
            ))

        has_auth = any(
            env_var in os.environ
            for env_var in ("PANEL_BASIC_AUTH", "PANEL_OAUTH_PROVIDER")
        ) or bool(_global_config.basic_auth or _global_config.oauth_provider)

        if not has_auth:
            issues.append(DiagnosticIssue(
                severity=_downgrade_for_dev(
                    DiagnosticSeverity.FATAL, mode, DiagnosticSeverity.WARNING,
                    steps=2,
                ),
                code="ADMIN_NO_AUTH",
                message="Admin panel is enabled without any authentication.",
                details=(
                    "The admin endpoint exposes sensitive server information "
                    "(session details, logs, profiling) and must be protected "
                    "by authentication."
                ),
                suggestion=(
                    "Enable Basic Auth via --basic-auth or PANEL_BASIC_AUTH, "
                    "or OAuth via --oauth-provider or PANEL_OAUTH_PROVIDER."
                ),
            ))

    status = _highest_severity([i.severity for i in issues]) if issues else DiagnosticSeverity.INFO
    return ServiceDiagnostic(
        name="admin_endpoint",
        enabled=enabled,
        status=status,
        issues=issues,
        config=service_config,
    )


def diagnose_session_cleanup(config: StartupConfig) -> ServiceDiagnostic:
    from ..config import config as _global_config

    issues: list[DiagnosticIssue] = []
    service_config: dict[str, t.Any] = {}
    mode = config.detect_mode()

    history = (
        config.session_history
        if config.session_history is not None
        else _global_config.session_history
    )
    enabled = history != 0

    service_config["session_history"] = history
    service_config["enabled"] = enabled
    if config.check_unused_sessions is not None:
        service_config["check_unused_sessions_ms"] = config.check_unused_sessions

    if history == -1:
        issues.append(DiagnosticIssue(
            severity=_downgrade_for_dev(
                DiagnosticSeverity.WARNING, mode, DiagnosticSeverity.INFO
            ),
            code="SESSION_HISTORY_UNLIMITED",
            message="Session history is set to unlimited (-1).",
            details=(
                "Unlimited session history will grow memory usage "
                "indefinitely as sessions accumulate."
            ),
            suggestion=(
                "Set a reasonable session history limit (e.g. 100) via "
                "--session-history or PANEL_SESSION_HISTORY."
            ),
        ))
    elif history > 10000:
        issues.append(DiagnosticIssue(
            severity=_downgrade_for_dev(
                DiagnosticSeverity.WARNING, mode, DiagnosticSeverity.INFO
            ),
            code="SESSION_HISTORY_LARGE",
            message=f"Session history limit is very large: {history}.",
            details=(
                "A large session history limit may consume significant "
                "memory."
            ),
            suggestion=(
                "Consider reducing the session history limit to a more "
                "reasonable value."
            ),
        ))
    elif history < -1:
        issues.append(DiagnosticIssue(
            severity=DiagnosticSeverity.ERROR,
            code="SESSION_HISTORY_INVALID",
            message=f"Invalid session history value: {history}.",
            details=(
                "Session history must be -1 (unlimited), 0 (disabled), or "
                "a positive integer."
            ),
            suggestion="Set session_history to -1, 0, or a positive integer.",
        ))

    if enabled and history != 0:
        try:
            from . import rest as _rest_module
            service_config["rest_api_available"] = True
        except Exception:
            service_config["rest_api_available"] = False
            issues.append(DiagnosticIssue(
                severity=_downgrade_for_dev(
                    DiagnosticSeverity.WARNING, mode, DiagnosticSeverity.INFO
                ),
                code="SESSION_HISTORY_REST_MISSING",
                message="REST API module not available for session info endpoint.",
                details=(
                    "Session history tracking requires the REST API module "
                    "to serve the /rest/session_info endpoint."
                ),
            ))

    status = _highest_severity([i.severity for i in issues]) if issues else DiagnosticSeverity.INFO
    return ServiceDiagnostic(
        name="session_cleanup",
        enabled=enabled,
        status=status,
        issues=issues,
        config=service_config,
    )


def diagnose_notifications(config: StartupConfig) -> ServiceDiagnostic:
    from ..config import config as _global_config

    issues: list[DiagnosticIssue] = []
    service_config: dict[str, t.Any] = {}
    mode = config.detect_mode()

    enabled = bool(_global_config.notifications)
    has_disconnect = bool(_global_config.disconnect_notification)
    has_ready = bool(_global_config.ready_notification)

    service_config["enabled"] = enabled
    service_config["disconnect_notification"] = has_disconnect
    service_config["ready_notification"] = has_ready

    if enabled:
        notification_module = None
        try:
            from .notifications import NotificationArea as _NA
            notification_module = _NA
        except Exception:
            pass

        service_config["notification_module_available"] = notification_module is not None
        if not notification_module:
            issues.append(DiagnosticIssue(
                severity=_downgrade_for_dev(DiagnosticSeverity.ERROR, mode),
                code="NOTIFICATIONS_MODULE_MISSING",
                message="Notifications module is not available.",
                details=(
                    "The notifications feature requires the NotificationArea "
                    "component to be importable."
                ),
            ))

        if has_disconnect and len(_global_config.disconnect_notification) > 500:
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.WARNING,
                code="NOTIFICATIONS_DISCONNECT_LONG",
                message="Disconnect notification message is very long.",
                details=(
                    f"Message length: "
                    f"{len(_global_config.disconnect_notification)} characters."
                ),
                suggestion=(
                    "Consider using a shorter disconnect notification "
                    "message."
                ),
            ))

        if has_ready and len(_global_config.ready_notification) > 500:
            issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.WARNING,
                code="NOTIFICATIONS_READY_LONG",
                message="Ready notification message is very long.",
                details=(
                    f"Message length: "
                    f"{len(_global_config.ready_notification)} characters."
                ),
                suggestion="Consider using a shorter ready notification message.",
            ))

    status = _highest_severity([i.severity for i in issues]) if issues else DiagnosticSeverity.INFO
    return ServiceDiagnostic(
        name="notifications",
        enabled=enabled,
        status=status,
        issues=issues,
        config=service_config,
    )


def diagnose_browser_info(config: StartupConfig) -> ServiceDiagnostic:
    from ..config import config as _global_config

    issues: list[DiagnosticIssue] = []
    service_config: dict[str, t.Any] = {}
    mode = config.detect_mode()

    enabled = bool(_global_config.browser_info)
    service_config["enabled"] = enabled

    if enabled:
        browser_module = None
        try:
            from .browser import BrowserInfo as _BI
            browser_module = _BI
        except Exception:
            pass

        service_config["browser_module_available"] = browser_module is not None
        if not browser_module:
            issues.append(DiagnosticIssue(
                severity=_downgrade_for_dev(
                    DiagnosticSeverity.WARNING, mode, DiagnosticSeverity.INFO
                ),
                code="BROWSER_INFO_MODULE_MISSING",
                message="Browser info module is not available.",
                details=(
                    "The browser_info feature requires the BrowserInfo "
                    "component to be importable."
                ),
            ))

        service_config["tracked_fields"] = [
            "dark_mode",
            "device_pixel_ratio",
            "language",
            "timezone",
            "timezone_offset",
            "webdriver",
            "webgl",
        ]

    status = _highest_severity([i.severity for i in issues]) if issues else DiagnosticSeverity.INFO
    return ServiceDiagnostic(
        name="browser_info",
        enabled=enabled,
        status=status,
        issues=issues,
        config=service_config,
    )


def run_startup_diagnostics(
    context: StartupConfig | None = None,
    **kwargs: t.Any,
) -> StartupDiagnosticResult:
    if context is None:
        context = StartupConfig.resolve(**kwargs)
    elif kwargs:
        for key, value in kwargs.items():
            if hasattr(context, key):
                setattr(context, key, value)

    mode = context.detect_mode()
    services: dict[str, ServiceDiagnostic] = {}
    global_issues: list[DiagnosticIssue] = []

    services["websocket_origin"] = diagnose_websocket_origin(context)
    services["static_dirs"] = diagnose_static_dirs(context)
    services["autoreload"] = diagnose_autoreload(context)
    services["admin_endpoint"] = diagnose_admin_endpoint(context)
    services["session_cleanup"] = diagnose_session_cleanup(context)
    services["notifications"] = diagnose_notifications(context)
    services["browser_info"] = diagnose_browser_info(context)

    for extra_check in context.extra_checks:
        try:
            result = extra_check()
            services[result.name] = result
        except Exception as e:
            global_issues.append(DiagnosticIssue(
                severity=DiagnosticSeverity.ERROR,
                code="EXTRA_CHECK_FAILED",
                message=f"Custom diagnostic check failed: {e!r}",
                details=str(e),
            ))

    all_severities: list[DiagnosticSeverity] = []
    for service in services.values():
        all_severities.append(service.status)
    for issue in global_issues:
        all_severities.append(issue.severity)

    overall_status = _highest_severity(all_severities)

    return StartupDiagnosticResult(
        timestamp=_get_timestamp(),
        startup_mode=mode,
        overall_status=overall_status,
        services=services,
        issues=global_issues,
    )


class StartupError(RuntimeError):
    def __init__(self, diagnostic_result: StartupDiagnosticResult):
        self.diagnostic_result = diagnostic_result
        report = diagnostic_result.format_report()
        super().__init__(
            "Panel server startup failed due to configuration errors.\n" + report
        )


def validate_startup(
    context: StartupConfig | None = None,
    *,
    blocking: bool = True,
    log_report: bool = True,
    **kwargs: t.Any,
) -> StartupDiagnosticResult:
    result = run_startup_diagnostics(context, **kwargs)

    if log_report:
        report_lines = result.format_report().split("\n")
        for line in report_lines:
            if result.has_fatal or result.has_errors:
                logger.error(line)
            elif result.has_warnings:
                logger.warning(line)
            else:
                logger.info(line)

    if blocking and (result.has_fatal or result.has_errors):
        raise StartupError(result)

    return result
