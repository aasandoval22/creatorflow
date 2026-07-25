from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from backend.services import autoclip_service
from backend.services.autoclip_service import (
    AutoClipServiceManager, CommandResult, ServiceManagerError,
)


class FakeCommands:
    def __init__(
        self, *, supported: bool = True, linger: str | None = "yes",
        systemd_result: CommandResult | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.supported = supported
        self.linger = linger
        self.systemd_result = systemd_result
        self.units = {
            "creatorflow-review.service": (
                "ActiveState=active\nSubState=running\nResult=success\n"
                "NextElapseUSecRealtime=\nUnitFileState=enabled\n"
            ),
            "creatorflow-production.timer": (
                "ActiveState=active\nSubState=waiting\nResult=success\n"
                "NextElapseUSecRealtime=Sat 2026-07-25 21:30:00 CDT\n"
                "NextElapseUSecMonotonic=5min\n"
                "UnitFileState=enabled\n"
            ),
            "creatorflow-production.service": (
                "ActiveState=inactive\nSubState=dead\nResult=success\n"
                "NextElapseUSecRealtime=\nUnitFileState=static\n"
            ),
        }

    def __call__(self, command):
        command = tuple(command)
        self.commands.append(command)
        if command[:3] == ("systemctl", "--user", "is-system-running"):
            if self.systemd_result is not None:
                return self.systemd_result
            return CommandResult(0 if self.supported else 3, "running\n", "no bus\n")
        if command[:2] == ("loginctl", "show-user"):
            if self.linger is None:
                return CommandResult(1, stderr="user unavailable")
            return CommandResult(0, self.linger + "\n")
        if command[:3] == ("systemctl", "--user", "show"):
            return CommandResult(0, self.units[command[3]])
        if command[:3] == ("systemctl", "--user", "list-timers"):
            return CommandResult(
                0,
                json.dumps([{"next": 1785015000000000}]),
            )
        if command[0] == "journalctl":
            return CommandResult(0, "recent service log\n")
        return CommandResult(0)


def manager(tmp_path: Path, commands: FakeCommands | None = None):
    root = tmp_path / "repo"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    commands = commands or FakeCommands()
    return AutoClipServiceManager(
        project_root=root,
        unit_directory=tmp_path / "units",
        environment_file=tmp_path / "config" / "creatorflow.env",
        template_directory=autoclip_service.TEMPLATE_DIRECTORY,
        command_runner=commands,
    ), commands


def test_service_generation_uses_absolute_paths_and_safe_behavior(tmp_path):
    service, _ = manager(tmp_path)
    units = service.render_units()
    production = units["creatorflow-production.service"]
    review = units["creatorflow-review.service"]
    assert str(service.project_root) in production
    assert str(service.project_root / ".venv" / "bin" / "python") in production
    assert "/usr/bin/python" not in production
    assert "-m backend.services.production_runner" in production
    assert "KillSignal=SIGINT" in production
    assert "OnActiveSec=5m" in units["creatorflow-production.timer"]
    assert review.index("$AUTOCLIP_REVIEW_ARGS") < review.index("--host 127.0.0.1")
    assert "Restart=on-failure" in review
    combined = "\n".join(units.values()).lower()
    assert "publish" not in combined
    assert "credential" not in combined
    assert "youtube upload" not in combined


@pytest.mark.parametrize("interval", ["30m", "2h", "1d", "45min"])
def test_configurable_schedule(tmp_path, interval):
    service, _ = manager(tmp_path)
    assert f"OnUnitInactiveSec={interval}" in service.render_units(interval)[
        "creatorflow-production.timer"
    ]


@pytest.mark.parametrize("interval", ["", "0m", "-1h", "every hour", "30"])
def test_invalid_schedule_rejected(tmp_path, interval):
    service, _ = manager(tmp_path)
    with pytest.raises(autoclip_service.argparse.ArgumentTypeError, match="interval"):
        service.render_units(interval)


def test_install_is_atomic_idempotent_and_creates_private_environment(tmp_path):
    service, commands = manager(tmp_path)
    first = service.install("30m")
    second = service.install("30m")
    assert sorted(first["changed_units"]) == sorted(autoclip_service.MANAGED_UNITS)
    assert second["changed_units"] == []
    assert service.environment_file.stat().st_mode & 0o777 == 0o600
    assert "AUTOCLIP_PRODUCTION_ARGS=" in service.environment_file.read_text()
    assert commands.commands.count(("systemctl", "--user", "daemon-reload")) == 2
    assert not list(service.unit_directory.glob(".*.tmp"))


def test_install_preserves_existing_environment_file(tmp_path):
    service, _ = manager(tmp_path)
    service.environment_file.parent.mkdir(parents=True)
    service.environment_file.write_text("AUTOCLIP_PRODUCTION_ARGS=--top 1\n")
    service.install()
    assert service.environment_file.read_text() == "AUTOCLIP_PRODUCTION_ARGS=--top 1\n"


def test_systemd_unavailable_is_actionable(tmp_path):
    service, _ = manager(tmp_path, FakeCommands(supported=False))
    with pytest.raises(ServiceManagerError, match="systemd user services are unavailable"):
        service.install()


def test_systemd_bus_failure_with_exit_one_is_not_mistaken_for_degraded(tmp_path):
    commands = FakeCommands(
        systemd_result=CommandResult(1, stderr="Failed to connect to bus")
    )
    service, _ = manager(tmp_path, commands)
    with pytest.raises(ServiceManagerError, match="Failed to connect to bus"):
        service.install()


def test_degraded_user_manager_remains_usable(tmp_path):
    commands = FakeCommands(systemd_result=CommandResult(1, stdout="degraded\n"))
    service, _ = manager(tmp_path, commands)
    service.check_support()


def test_missing_virtual_environment_is_actionable(tmp_path):
    commands = FakeCommands()
    service = AutoClipServiceManager(
        project_root=tmp_path / "repo",
        unit_directory=tmp_path / "units",
        environment_file=tmp_path / "config" / "env",
        command_runner=commands,
    )
    with pytest.raises(ServiceManagerError, match="Python is missing"):
        service.start()


def test_lingering_disabled_is_reported_without_changing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "test-user")
    service, commands = manager(tmp_path, FakeCommands(linger="no"))
    output = StringIO()
    assert autoclip_service.main(
        ["install"], manager=service, stdout=output, stderr=StringIO()
    ) == 0
    assert "sudo loginctl enable-linger test-user" in output.getvalue()
    assert not any(command[:2] == ("loginctl", "enable-linger") for command in commands.commands)


def test_lingering_unavailable_is_reported(tmp_path):
    service, _ = manager(tmp_path, FakeCommands(linger=None))
    assert service.lingering() == "unavailable"


def test_management_command_construction(tmp_path):
    service, commands = manager(tmp_path)
    service.start()
    service.stop()
    service.restart()
    service.disable()
    service.run_now()
    assert (
        "systemctl", "--user", "enable", "--now",
        "creatorflow-review.service", "creatorflow-production.timer",
    ) in commands.commands
    assert (
        "systemctl", "--user", "disable", "--now",
        "creatorflow-production.timer",
    ) in commands.commands
    assert (
        "systemctl", "--user", "stop", "creatorflow-production.timer",
        "creatorflow-production.service", "creatorflow-review.service",
    ) in commands.commands
    assert (
        "systemctl", "--user", "start", "creatorflow-production.service",
    ) in commands.commands


def test_status_and_health_reporting(tmp_path):
    service, _ = manager(tmp_path)
    log = tmp_path / "production.jsonl"
    log.write_text(
        "\n".join(
            (
                json.dumps({"timestamp": "2026-07-25T20:00:00Z", "event": "run_summary", "failures": 0}),
                json.dumps({"timestamp": "2026-07-25T20:30:00Z", "event": "run_failed"}),
            )
        ) + "\n"
    )
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"updated_at": "2026-07-25T20:30:01Z"}))
    queue = tmp_path / "reviews.json"
    queue.write_text(json.dumps({"items": [{"status": "pending"}, {"status": "approved"}]}))
    status = service.status(
        state_path=state, log_path=log, review_queue_path=queue,
    )
    assert status["review_server"]["active"] == "active"
    assert status["production_timer"]["sub_state"] == "waiting"
    assert status["next_scheduled_run"] == "2026-07-25T21:30:00Z"
    assert status["most_recent_production_result"] == "success"
    assert status["most_recent_success"] == "2026-07-25T20:00:00Z"
    assert status["most_recent_failure"] == "2026-07-25T20:30:00Z"
    assert status["processing_state_updated_at"] == "2026-07-25T20:30:01Z"
    assert status["awaiting_review"] == 1


def test_status_handles_missing_or_malformed_runtime_files(tmp_path):
    service, _ = manager(tmp_path)
    malformed = tmp_path / "bad.json"
    malformed.write_text("{")
    status = service.status(
        state_path=malformed, log_path=tmp_path / "missing.log",
        review_queue_path=malformed,
    )
    assert status["most_recent_success"] is None
    assert status["processing_state_updated_at"] is None
    assert status["awaiting_review"] is None


def test_logs_uses_user_journal(tmp_path):
    service, commands = manager(tmp_path)
    assert service.logs(25).stdout == "recent service log\n"
    assert commands.commands[-1] == (
        "journalctl", "--user", "--no-pager", "-n", "25",
        "-u", "creatorflow-production.service",
        "-u", "creatorflow-production.timer",
        "-u", "creatorflow-review.service",
    )


def test_cli_status_output(tmp_path, monkeypatch):
    service, _ = manager(tmp_path)
    original_status = service.status
    monkeypatch.setattr(
        service,
        "status",
        lambda: original_status(
            state_path=tmp_path / "state.json",
            log_path=tmp_path / "production.jsonl",
            review_queue_path=tmp_path / "reviews.json",
        ),
    )
    output = StringIO()
    assert autoclip_service.main(
        ["status"], manager=service, stdout=output, stderr=StringIO(),
    ) == 0
    rendered = output.getvalue()
    assert "Review server: active" in rendered
    assert "Production timer: active" in rendered
    assert "Awaiting review: 0" in rendered


def test_cli_returns_nonzero_on_service_error(tmp_path):
    service, _ = manager(tmp_path, FakeCommands(supported=False))
    error = StringIO()
    assert autoclip_service.main(
        ["start"], manager=service, stdout=StringIO(), stderr=error,
    ) == 1
    assert "systemd user services are unavailable" in error.getvalue()
