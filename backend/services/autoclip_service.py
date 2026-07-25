"""Install and manage CreatorFlow systemd user services."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from backend.services.clip_review_queue import DEFAULT_REVIEW_QUEUE_PATH
from backend.services.production_deployment import (
    DeploymentError, ProcessResult, ProductionDeployer,
)
from backend.services.production_runner import DEFAULT_LOG_PATH, DEFAULT_STATE_PATH


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEVELOPMENT_ROOT = Path.home() / "clip-factory"
DEFAULT_PRODUCTION_ROOT = Path.home() / "clip-factory-production"
DEFAULT_RUNTIME_ROOT = DEFAULT_PRODUCTION_ROOT / "current"
TEMPLATE_DIRECTORY = PROJECT_ROOT / "deploy" / "systemd"
DEFAULT_USER_CONFIG = Path.home() / ".config"
DEFAULT_UNIT_DIRECTORY = DEFAULT_USER_CONFIG / "systemd" / "user"
DEFAULT_ENVIRONMENT_FILE = DEFAULT_USER_CONFIG / "creatorflow" / "creatorflow.env"
PRODUCTION_SERVICE = "creatorflow-production.service"
PRODUCTION_TIMER = "creatorflow-production.timer"
REVIEW_SERVICE = "creatorflow-review.service"
MANAGED_UNITS = (PRODUCTION_SERVICE, PRODUCTION_TIMER, REVIEW_SERVICE)
DEFAULT_INTERVAL = "30m"
INTERVAL_PATTERN = re.compile(r"^[1-9][0-9]*(?:s|min|m|h|d|w)$")


class ServiceManagerError(RuntimeError):
    """Actionable service-management failure."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str]], CommandResult]


def run_command(command: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command), check=False, text=True, capture_output=True,
        )
    except OSError as error:
        raise ServiceManagerError(
            f"Cannot run {command[0]!r}: {error}."
        ) from error
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def validate_interval(value: str) -> str:
    value = value.strip()
    if not INTERVAL_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "interval must be a positive systemd duration such as 30m, 2h, or 1d"
        )
    return value


def _escape_systemd_path(path: Path) -> str:
    value = os.path.abspath(path)
    if any(character in value for character in "\n\r\0"):
        raise ServiceManagerError(f"Unsafe path for systemd unit: {value!r}.")
    return value.replace("\\", "\\\\").replace(" ", "\\x20")


def _atomic_write(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


class AutoClipServiceManager:
    def __init__(
        self, *, project_root: Path = DEFAULT_RUNTIME_ROOT,
        unit_directory: Path = DEFAULT_UNIT_DIRECTORY,
        environment_file: Path = DEFAULT_ENVIRONMENT_FILE,
        template_directory: Path | None = None,
        support_python_path: Path | None = None,
        command_runner: CommandRunner = run_command,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.python_path = self.project_root / ".venv" / "bin" / "python"
        self.support_python_path = Path(
            support_python_path or self.python_path
        ).resolve()
        self.unit_directory = Path(unit_directory).resolve()
        self.environment_file = Path(environment_file).resolve()
        self.template_directory = Path(
            template_directory or self.project_root / "deploy" / "systemd"
        ).resolve()
        self.command_runner = command_runner

    def check_support(self) -> None:
        if not self.support_python_path.is_file():
            raise ServiceManagerError(
                "Production virtual-environment Python is missing: "
                f"{self.support_python_path}."
            )
        result = self.command_runner(("systemctl", "--user", "is-system-running"))
        state = result.stdout.strip()
        if result.returncode != 0 and not (
            result.returncode == 1 and state == "degraded"
        ):
            detail = (result.stderr or result.stdout).strip()
            raise ServiceManagerError(
                "systemd user services are unavailable"
                + (f": {detail}" if detail else ".")
            )

    def lingering(self) -> str:
        user = os.environ.get("USER") or Path.home().name
        result = self.command_runner(("loginctl", "show-user", user, "-p", "Linger", "--value"))
        if result.returncode != 0:
            return "unavailable"
        value = result.stdout.strip().lower()
        return value if value in {"yes", "no"} else "unavailable"

    def render_units(self, interval: str = DEFAULT_INTERVAL) -> dict[str, str]:
        validate_interval(interval)
        replacements = {
            "project_root": _escape_systemd_path(self.project_root),
            "python_path": _escape_systemd_path(self.python_path),
            "environment_file": _escape_systemd_path(self.environment_file),
            "interval": interval,
        }
        rendered: dict[str, str] = {}
        for unit in MANAGED_UNITS:
            template_path = self.template_directory / f"{unit}.in"
            try:
                template = template_path.read_text(encoding="utf-8")
            except OSError as error:
                raise ServiceManagerError(
                    f"Cannot read unit template {template_path}: {error}."
                ) from error
            rendered[unit] = template.format(**replacements)
        return rendered

    def install(self, interval: str = DEFAULT_INTERVAL) -> dict[str, Any]:
        self.check_support()
        changed: list[str] = []
        for name, content in self.render_units(interval).items():
            if _atomic_write(self.unit_directory / name, content):
                changed.append(name)
        if not self.environment_file.exists():
            _atomic_write(
                self.environment_file,
                "# Local CreatorFlow service options; do not commit this file.\n"
                "AUTOCLIP_PRODUCTION_ARGS=\n"
                "AUTOCLIP_REVIEW_ARGS=\n",
            )
        try:
            self.environment_file.chmod(0o600)
        except OSError as error:
            raise ServiceManagerError(
                f"Cannot secure environment file {self.environment_file}: {error}."
            ) from error
        self._require_success(("systemctl", "--user", "daemon-reload"))
        return {
            "changed_units": changed,
            "unit_directory": str(self.unit_directory),
            "environment_file": str(self.environment_file),
            "interval": interval,
            "linger": self.lingering(),
        }

    def _deployment_status(self) -> dict[str, Any]:
        return ProductionDeployer(
            development_root=DEFAULT_DEVELOPMENT_ROOT,
            production_root=DEFAULT_PRODUCTION_ROOT,
            runner=self._deployment_command,
        ).status()

    def _deployment_command(
        self, command: Sequence[str], _cwd: Path | None = None,
    ) -> ProcessResult:
        result = self.command_runner(command)
        return ProcessResult(result.returncode, result.stdout, result.stderr)

    def start(self) -> None:
        self.check_support()
        self._require_success(
            ("systemctl", "--user", "enable", "--now", REVIEW_SERVICE, PRODUCTION_TIMER)
        )

    def stop(self) -> None:
        self.check_support()
        self._require_success(
            (
                "systemctl", "--user", "stop", PRODUCTION_TIMER,
                PRODUCTION_SERVICE, REVIEW_SERVICE,
            )
        )

    def restart(self) -> None:
        self.check_support()
        self._require_success(
            ("systemctl", "--user", "restart", REVIEW_SERVICE, PRODUCTION_TIMER)
        )

    def disable(self) -> None:
        self.check_support()
        self._require_success(
            ("systemctl", "--user", "disable", "--now", PRODUCTION_TIMER)
        )

    def run_now(self) -> None:
        self.check_support()
        self._require_success(
            ("systemctl", "--user", "start", PRODUCTION_SERVICE)
        )

    def logs(self, lines: int = 100) -> CommandResult:
        self.check_support()
        result = self.command_runner(
            (
                "journalctl", "--user", "--no-pager", "-n", str(lines),
                "-u", PRODUCTION_SERVICE, "-u", PRODUCTION_TIMER,
                "-u", REVIEW_SERVICE,
            )
        )
        if result.returncode != 0:
            raise ServiceManagerError(
                f"journalctl failed: {(result.stderr or result.stdout).strip()}."
            )
        return result

    def status(
        self, *, state_path: Path = DEFAULT_STATE_PATH,
        log_path: Path = DEFAULT_LOG_PATH,
        review_queue_path: Path = DEFAULT_REVIEW_QUEUE_PATH,
    ) -> dict[str, Any]:
        self.check_support()
        review = self._unit_status(REVIEW_SERVICE)
        timer = self._unit_status(PRODUCTION_TIMER)
        production = self._unit_status(PRODUCTION_SERVICE)
        history = _production_history(Path(log_path))
        return {
            "review_server": review,
            "production_timer": timer,
            "production_service": production,
            "next_scheduled_run": self._next_timer_run(),
            "most_recent_production_result": production.get("result"),
            "most_recent_success": history["success"],
            "most_recent_failure": history["failure"],
            "processing_state_updated_at": _state_updated_at(Path(state_path)),
            "awaiting_review": _pending_review_count(Path(review_queue_path)),
            "linger": self.lingering(),
            "deployment": self._deployment_status(),
        }

    def _next_timer_run(self) -> str | None:
        result = self.command_runner(
            (
                "systemctl", "--user", "list-timers", PRODUCTION_TIMER,
                "--no-pager", "--output=json",
            )
        )
        if result.returncode != 0:
            return None
        try:
            timers = json.loads(result.stdout)
            value = timers[0]["next"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return datetime.fromtimestamp(
                value / 1_000_000, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _unit_status(self, unit: str) -> dict[str, Any]:
        properties = (
            "ActiveState", "SubState", "Result", "NextElapseUSecRealtime",
            "NextElapseUSecMonotonic", "UnitFileState",
        )
        result = self.command_runner(
            ("systemctl", "--user", "show", unit, "--property", ",".join(properties))
        )
        if result.returncode != 0:
            return {
                "active": "unavailable",
                "detail": (result.stderr or result.stdout).strip() or "unit unavailable",
            }
        values = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value or None
        return {
            "active": values.get("ActiveState"),
            "sub_state": values.get("SubState"),
            "enabled": values.get("UnitFileState"),
            "result": values.get("Result"),
            "next_elapse": (
                values.get("NextElapseUSecRealtime")
                or values.get("NextElapseUSecMonotonic")
            ),
        }

    def _require_success(self, command: Sequence[str]) -> CommandResult:
        result = self.command_runner(command)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ServiceManagerError(
                f"{' '.join(command)} failed"
                + (f": {detail}" if detail else ".")
            )
        return result


def _production_history(path: Path) -> dict[str, str | None]:
    success = failure = None
    if not path.exists():
        return {"success": success, "failure": failure}
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = event.get("timestamp")
                if not isinstance(timestamp, str):
                    continue
                if event.get("event") == "run_summary":
                    if event.get("failures") == 0:
                        success = timestamp
                    else:
                        failure = timestamp
                elif event.get("event") == "run_failed":
                    failure = timestamp
    except OSError:
        return {"success": None, "failure": None}
    return {"success": success, "failure": failure}


def _state_updated_at(path: Path) -> str | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = document.get("updated_at") if isinstance(document, dict) else None
    return value if isinstance(value, str) else None


def _pending_review_count(path: Path) -> int | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, json.JSONDecodeError):
        return None
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        return None
    return sum(
        1 for item in items
        if isinstance(item, dict) and item.get("status") == "pending"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage CreatorFlow systemd user automation."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--interval", type=validate_interval, default=DEFAULT_INTERVAL)
    for name in ("start", "stop", "restart", "status", "run-now", "disable"):
        commands.add_parser(name)
    deploy = commands.add_parser("deploy")
    deploy.add_argument("--interval", type=validate_interval, default=DEFAULT_INTERVAL)
    commands.add_parser("rollback")
    logs = commands.add_parser("logs")
    logs.add_argument("--lines", type=int, default=100)
    return parser


def _print_status(status: Mapping[str, Any], stream: TextIO) -> None:
    review = status["review_server"]
    timer = status["production_timer"]
    print(f"Review server: {review.get('active', 'unavailable')}", file=stream)
    print(f"Production timer: {timer.get('active', 'unavailable')}", file=stream)
    print(f"Next production run: {status['next_scheduled_run'] or 'unavailable'}", file=stream)
    print(
        f"Most recent production result: "
        f"{status['most_recent_production_result'] or 'unavailable'}",
        file=stream,
    )
    print(f"Most recent successful run: {status['most_recent_success'] or 'unavailable'}", file=stream)
    print(f"Most recent failure: {status['most_recent_failure'] or 'none recorded'}", file=stream)
    pending = status["awaiting_review"]
    print(f"Awaiting review: {pending if pending is not None else 'unavailable'}", file=stream)
    print(f"User lingering: {status['linger']}", file=stream)
    deployment = status.get("deployment") or {}
    print(
        f"Deployed commit: {deployment.get('deployed_commit') or 'none'}",
        file=stream,
    )
    print(
        f"Origin main commit: {deployment.get('origin_main_commit') or 'unavailable'}",
        file=stream,
    )
    behind = deployment.get("production_behind_main")
    print(
        "Production behind main: "
        + ("unknown" if behind is None else ("yes" if behind else "no")),
        file=stream,
    )


def main(
    argv: Sequence[str] | None = (), *,
    manager: AutoClipServiceManager | None = None,
    deployer: ProductionDeployer | None = None,
    stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    manager = manager or AutoClipServiceManager()
    try:
        if args.command == "install":
            result = manager.install(args.interval)
            changed = ", ".join(result["changed_units"]) or "none (already current)"
            print(f"Installed units: {changed}", file=stdout)
            print(f"Unit directory: {result['unit_directory']}", file=stdout)
            print(f"Environment file: {result['environment_file']}", file=stdout)
            print(f"Production interval: {result['interval']}", file=stdout)
            if result["linger"] == "no":
                user = os.environ.get("USER") or Path.home().name
                print(
                    "Persistent user services after logout/reboot require a one-time "
                    f"administrator command: sudo loginctl enable-linger {user}",
                    file=stdout,
                )
        elif args.command == "start":
            manager.start()
            print("Review server and production timer started.", file=stdout)
        elif args.command == "stop":
            manager.stop()
            print("Review server and production timer stopped.", file=stdout)
        elif args.command == "restart":
            manager.restart()
            print("Review server and production timer restarted.", file=stdout)
        elif args.command == "disable":
            manager.disable()
            print("Automatic production disabled; review server was not changed.", file=stdout)
        elif args.command == "run-now":
            manager.run_now()
            print("Production cycle completed.", file=stdout)
        elif args.command == "logs":
            if args.lines < 1:
                raise ServiceManagerError("--lines must be positive.")
            print(manager.logs(args.lines).stdout, end="", file=stdout)
        elif args.command == "status":
            _print_status(manager.status(), stdout)
        elif args.command in {"deploy", "rollback"}:
            deployer = deployer or ProductionDeployer(development_root=PROJECT_ROOT)
            result = (
                deployer.deploy(interval=args.interval)
                if args.command == "deploy" else deployer.rollback()
            )
            print(json.dumps(result, indent=2, sort_keys=True), file=stdout)
    except (ServiceManagerError, DeploymentError) as error:
        print(f"CreatorFlow service error: {error}", file=stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
