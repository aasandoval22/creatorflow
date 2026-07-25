from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.services import autoclip_service
from backend.services.production_deployment import (
    DeploymentError, ProcessResult, ProductionDeployer,
)


COMMIT = "a" * 40
PREVIOUS = "b" * 40


class FakeProcesses:
    def __init__(self, *, branch="main", dirty=False, origin=COMMIT, head=COMMIT):
        self.branch = branch
        self.dirty = dirty
        self.origin = origin
        self.head = head
        self.commands = []
        self.active = set()
        self.fail_restart = False

    def __call__(self, command, cwd=None):
        command = tuple(command)
        self.commands.append((command, cwd))
        if command[:2] == ("git", "status"):
            return ProcessResult(0, " M README.md\n" if self.dirty else "")
        if command[:3] == ("git", "branch", "--show-current"):
            return ProcessResult(0, self.branch + "\n")
        if command[:3] == ("git", "rev-parse", "origin/main"):
            return ProcessResult(0, self.origin + "\n")
        if command[:3] == ("git", "rev-parse", "HEAD"):
            return ProcessResult(0, self.head + "\n")
        if command[:3] == ("systemctl", "--user", "is-active"):
            return ProcessResult(0 if command[3] in self.active else 3, "active\n" if command[3] in self.active else "inactive\n")
        if command[:3] == ("systemctl", "--user", "restart") and self.fail_restart:
            return ProcessResult(1, stderr="restart failed")
        if command[:3] == ("systemctl", "--user", "is-system-running"):
            return ProcessResult(0, "running\n")
        if command[:2] == ("loginctl", "show-user"):
            return ProcessResult(0, "yes\n")
        return ProcessResult(0)


class SimulatedDeployer(ProductionDeployer):
    fail_validation = False

    def _prepare_release(self, commit):
        release = self.releases_root / commit
        if (release / "deployment.json").is_file():
            self._validate_release(release)
            return release, False
        release.mkdir(parents=True)
        (release / "deploy" / "systemd").mkdir(parents=True)
        for template in autoclip_service.TEMPLATE_DIRECTORY.glob("*.in"):
            (release / "deploy" / "systemd" / template.name).write_text(
                template.read_text(encoding="utf-8"), encoding="utf-8"
            )
        python = release / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        (release / "data").symlink_to(self.data_root)
        (release / "deployment.json").write_text(
            json.dumps({"version": 1, "commit": commit}), encoding="utf-8"
        )
        self._validate_release(release)
        return release, True

    def _validate_release(self, release, *, expected_commit=None):
        if self.fail_validation:
            raise DeploymentError("simulated validation failure")
        if expected_commit is not None:
            assert release.name == expected_commit
        assert (release / ".venv" / "bin" / "python").is_file()
        assert (release / "data").resolve() == self.data_root


def make_deployer(tmp_path, processes=None, cls=SimulatedDeployer):
    development = tmp_path / "clip-factory"
    development.mkdir()
    (development / "data").mkdir()
    (development / ".venv" / "bin").mkdir(parents=True)
    (development / ".venv" / "bin" / "python").symlink_to(sys.executable)
    environment = tmp_path / "config" / "creatorflow.env"
    environment.parent.mkdir()
    environment.write_text("AUTOCLIP_PRODUCTION_ARGS=\n", encoding="utf-8")
    environment.chmod(0o600)
    deno = tmp_path / ".deno" / "bin" / "deno"
    deno.parent.mkdir(parents=True)
    deno.write_text("#!/bin/sh\n", encoding="utf-8")
    deno.chmod(0o700)
    processes = processes or FakeProcesses()
    return cls(
        development_root=development,
        production_root=tmp_path / "clip-factory-production",
        persistent_root=tmp_path / "persistent",
        environment_file=environment,
        unit_directory=tmp_path / "units",
        runner=processes,
        python_executable=development / ".venv" / "bin" / "python",
        deno_path=deno,
    ), processes


def test_refuses_dirty_worktree(tmp_path):
    deployer, _ = make_deployer(tmp_path, FakeProcesses(dirty=True))
    with pytest.raises(DeploymentError, match="clean development worktree"):
        deployer.deploy()


def test_refuses_non_main_branch(tmp_path):
    deployer, _ = make_deployer(
        tmp_path, FakeProcesses(branch="codex/feature")
    )
    with pytest.raises(DeploymentError, match="branch main"):
        deployer.deploy()


def test_refuses_local_main_that_is_not_exact_origin_main(tmp_path):
    deployer, _ = make_deployer(
        tmp_path, FakeProcesses(head=PREVIOUS)
    )
    with pytest.raises(DeploymentError, match="does not match origin/main"):
        deployer.deploy()


def test_symlinked_virtual_environment_launcher_path_is_preserved(tmp_path):
    deployer, _ = make_deployer(tmp_path)
    launcher = deployer.development_root / ".venv" / "bin" / "python"
    assert launcher.is_symlink()
    assert launcher.resolve() != launcher
    assert deployer.python_executable == launcher
    deployer._ensure_development_launcher()


def test_regular_executable_launcher_path_is_preserved(tmp_path):
    deployer, _ = make_deployer(tmp_path)
    launcher = deployer.development_root / ".venv" / "bin" / "python"
    launcher.unlink()
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    assert deployer.python_executable == launcher
    deployer._ensure_development_launcher()


@pytest.mark.parametrize("present", [False, True])
def test_missing_or_non_executable_launcher_fails_before_data_migration(
    tmp_path, present,
):
    deployer, _ = make_deployer(tmp_path)
    launcher = deployer.development_root / ".venv" / "bin" / "python"
    launcher.unlink()
    if present:
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o600)
    with pytest.raises(
        DeploymentError, match="not executable" if present else "is missing"
    ):
        deployer.deploy()
    assert (deployer.development_root / "data").is_dir()
    assert not deployer.data_root.exists()


def test_deploy_isolated_exact_commit_and_migrates_persistent_data(tmp_path):
    deployer, processes = make_deployer(tmp_path)
    original_data = deployer.development_root / "data"
    (original_data / "state.json").write_text("preserved", encoding="utf-8")
    result = deployer.deploy()
    release = Path(result["release_path"])
    assert result["deployed_commit"] == COMMIT
    assert deployer.current_link.resolve() == release
    assert release != deployer.development_root
    assert (release / ".venv").is_dir()
    assert (release / "data").is_symlink()
    assert (deployer.data_root / "state.json").read_text() == "preserved"
    assert (deployer.development_root / "data").is_symlink()
    assert deployer.environment_file.read_text() == "AUTOCLIP_PRODUCTION_ARGS=\n"
    assert deployer.environment_file.stat().st_mode & 0o777 == 0o600
    assert result["restarted_units"] == []
    assert any(command[:3] == ("git", "fetch", "--prune") for command, _ in processes.commands)


def test_generated_units_only_reference_isolated_current_path(tmp_path):
    deployer, _ = make_deployer(tmp_path)
    deployer.unit_directory.mkdir()
    for unit in autoclip_service.MANAGED_UNITS:
        (deployer.unit_directory / unit).write_text(
            f"WorkingDirectory={deployer.development_root}\n",
            encoding="utf-8",
        )
    deployer.deploy()
    combined = "\n".join(
        (deployer.unit_directory / unit).read_text(encoding="utf-8")
        for unit in autoclip_service.MANAGED_UNITS
    )
    assert str(deployer.current_link) in combined
    assert str(deployer.current_link / ".venv" / "bin" / "python") in combined
    assert str(deployer.development_root) + os.sep not in combined
    assert "127.0.0.1" in combined
    assert "publish" not in combined.lower()


def test_repeat_deployment_same_commit_is_idempotent(tmp_path):
    deployer, _ = make_deployer(tmp_path)
    first = deployer.deploy()
    second = deployer.deploy()
    assert first["created_release"] is True
    assert second["created_release"] is False
    assert second["deployed_commit"] == COMMIT
    assert second["previous_commit"] == COMMIT
    assert deployer.current_link.resolve() == Path(first["release_path"])
    assert deployer.data_root.stat().st_ino == (
        deployer.development_root / "data"
    ).stat().st_ino


def test_service_manager_preserves_stable_current_symlink_path(tmp_path):
    release = tmp_path / "production" / "releases" / COMMIT
    release.mkdir(parents=True)
    current = tmp_path / "production" / "current"
    current.symlink_to(release)
    service = autoclip_service.AutoClipServiceManager(
        project_root=current,
        template_directory=autoclip_service.TEMPLATE_DIRECTORY,
    )
    assert service.project_root == current
    assert service.python_path == current / ".venv" / "bin" / "python"


class PreparationProcesses(FakeProcesses):
    def __init__(self, *, fail_install=False):
        super().__init__()
        self.fail_install = fail_install

    def __call__(self, command, cwd=None):
        command = tuple(command)
        if command[:3] == ("git", "worktree", "add"):
            release = Path(command[-2])
            release.mkdir(parents=True)
            requirements = release / "backend" / "requirements-transcription.txt"
            requirements.parent.mkdir()
            requirements.write_text("-r requirements.txt\n", encoding="utf-8")
            (release / "backend" / "requirements.txt").write_text(
                "yt-dlp\n", encoding="utf-8"
            )
            self.commands.append((command, cwd))
            return ProcessResult(0)
        if len(command) >= 4 and command[1:3] == ("-m", "venv"):
            python = Path(command[3]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            self.commands.append((command, cwd))
            return ProcessResult(0)
        if len(command) >= 4 and command[1:4] == ("-m", "pip", "install"):
            self.commands.append((command, cwd))
            return ProcessResult(
                1 if self.fail_install else 0,
                stderr="offline install failure" if self.fail_install else "",
            )
        if command[:4] == ("git", "worktree", "remove", "--force"):
            shutil.rmtree(Path(command[4]), ignore_errors=True)
            self.commands.append((command, cwd))
            return ProcessResult(0)
        if command[:3] == ("git", "rev-parse", "HEAD") and cwd is not None:
            return ProcessResult(0, COMMIT + "\n")
        return super().__call__(command, cwd)


def test_release_preparation_mocks_dependencies_and_validates_exact_commit(tmp_path):
    processes = PreparationProcesses()
    deployer, _ = make_deployer(
        tmp_path, processes, cls=ProductionDeployer
    )
    deployer._prepare_persistent_data()
    release, created = deployer._prepare_release(COMMIT)
    assert created is True
    assert json.loads((release / "deployment.json").read_text())["commit"] == COMMIT
    commands = [command for command, _ in processes.commands]
    assert any(command[:3] == ("git", "worktree", "add") for command in commands)
    assert any(command[1:4] == ("-m", "pip", "install") for command in commands)
    test_command = next(
        command for command in commands if command[1:3] == ("-m", "pytest")
    )
    assert test_command[0] == str(
        deployer.development_root / ".venv" / "bin" / "python"
    )


def test_dependency_failure_removes_incomplete_release(tmp_path):
    processes = PreparationProcesses(fail_install=True)
    deployer, _ = make_deployer(
        tmp_path, processes, cls=ProductionDeployer
    )
    deployer._prepare_persistent_data()
    marker = deployer.data_root / "preserved.txt"
    marker.write_text("persistent", encoding="utf-8")
    with pytest.raises(DeploymentError, match="dependency installation"):
        deployer._prepare_release(COMMIT)
    assert not (deployer.releases_root / COMMIT).exists()
    assert marker.read_text(encoding="utf-8") == "persistent"
    assert not any(
        command[:3] == ("systemctl", "--user", "restart")
        for command, _ in processes.commands
    )


def test_validation_failure_preserves_previous_release(tmp_path):
    deployer, _ = make_deployer(tmp_path)
    previous = deployer.releases_root / PREVIOUS
    previous.mkdir(parents=True)
    (previous / ".venv" / "bin").mkdir(parents=True)
    (previous / ".venv" / "bin" / "python").symlink_to(sys.executable)
    deployer._prepare_persistent_data()
    (previous / "data").symlink_to(deployer.data_root)
    deployer.current_link.parent.mkdir(parents=True, exist_ok=True)
    deployer.current_link.symlink_to(previous)
    deployer.fail_validation = True
    with pytest.raises(DeploymentError, match="validation failure"):
        deployer.deploy()
    assert deployer.current_link.resolve() == previous


def test_activation_failure_restores_previous_release_and_state(tmp_path):
    processes = FakeProcesses(origin=PREVIOUS, head=PREVIOUS)
    deployer, _ = make_deployer(tmp_path, processes)
    deployer.deploy()
    previous_target = deployer.current_link.resolve()
    previous_state = deployer.state_path.read_bytes()
    processes.origin = COMMIT
    processes.head = COMMIT
    processes.active.add("creatorflow-review.service")
    processes.fail_restart = True
    with pytest.raises(DeploymentError, match="restart failed"):
        deployer.deploy()
    assert deployer.current_link.resolve() == previous_target
    assert deployer.state_path.read_bytes() == previous_state


def test_status_reports_deployed_commit_and_behind_main(tmp_path):
    deployer, processes = make_deployer(tmp_path)
    deployer.production_root.mkdir()
    deployer.state_path.write_text(
        json.dumps({"deployed_commit": PREVIOUS}), encoding="utf-8"
    )
    status = deployer.status()
    assert status["deployed_commit"] == PREVIOUS
    assert status["origin_main_commit"] == COMMIT
    assert status["production_behind_main"] is True
    processes.origin = PREVIOUS
    assert deployer.status()["production_behind_main"] is False


def test_development_branch_change_does_not_change_production_target(tmp_path):
    deployer, processes = make_deployer(tmp_path)
    deployer.deploy()
    release = deployer.current_link.resolve()
    processes.branch = "codex/another-feature"
    assert deployer.current_link.resolve() == release
    assert release != deployer.development_root


def test_environment_permissions_and_deno_are_required(tmp_path):
    deployer, _ = make_deployer(tmp_path)
    deployer.environment_file.chmod(0o644)
    with pytest.raises(DeploymentError, match="mode 0600"):
        deployer.deploy()
    deployer.environment_file.chmod(0o600)
    deployer.deno_path.unlink()
    release = deployer.releases_root / COMMIT
    release.mkdir(parents=True)
    (release / ".venv" / "bin").mkdir(parents=True)
    (release / ".venv" / "bin" / "python").symlink_to(sys.executable)
    deployer._prepare_persistent_data()
    (release / "data").symlink_to(deployer.data_root)
    with pytest.raises(DeploymentError, match="Deno runtime"):
        ProductionDeployer._validate_release(deployer, release)


def test_rollback_atomically_selects_previous_release(tmp_path):
    deployer, _ = make_deployer(tmp_path)
    deployer.deploy()
    current = deployer.current_link.resolve()
    previous = deployer.releases_root / PREVIOUS
    previous.mkdir()
    (previous / ".venv" / "bin").mkdir(parents=True)
    (previous / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (previous / "data").symlink_to(deployer.data_root)
    for template in autoclip_service.TEMPLATE_DIRECTORY.glob("*.in"):
        target = previous / "deploy" / "systemd"
        target.mkdir(parents=True, exist_ok=True)
        (target / template.name).write_text(template.read_text(), encoding="utf-8")
    state = json.loads(deployer.state_path.read_text())
    state["previous_commit"] = PREVIOUS
    deployer.state_path.write_text(json.dumps(state), encoding="utf-8")
    result = deployer.rollback()
    assert result["deployed_commit"] == PREVIOUS
    assert result["previous_commit"] == COMMIT
    assert deployer.current_link.resolve() == previous
    assert deployer.current_link.resolve() != current


class FakeCliDeployer:
    def deploy(self, *, interval):
        return {"deployed_commit": COMMIT, "interval": interval}

    def rollback(self):
        return {"deployed_commit": PREVIOUS}


def test_cli_deploy_and_rollback_accept_explicit_dependencies(tmp_path):
    service, _ = autoclip_service_test_manager(tmp_path)
    output = []
    from io import StringIO

    stream = StringIO()
    assert autoclip_service.main(
        ["deploy", "--interval", "2h"], manager=service,
        deployer=FakeCliDeployer(), stdout=stream,
    ) == 0
    output.append(stream.getvalue())
    stream = StringIO()
    assert autoclip_service.main(
        ["rollback"], manager=service, deployer=FakeCliDeployer(), stdout=stream,
    ) == 0
    output.append(stream.getvalue())
    assert COMMIT in output[0] and PREVIOUS in output[1]


def autoclip_service_test_manager(tmp_path):
    root = tmp_path / "runtime"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)

    def command(_command):
        return autoclip_service.CommandResult(0, "running\n")

    return autoclip_service.AutoClipServiceManager(
        project_root=root,
        unit_directory=tmp_path / "unused-units",
        environment_file=tmp_path / "unused-env",
        template_directory=autoclip_service.TEMPLATE_DIRECTORY,
        command_runner=command,
    ), command


def test_repository_ignore_rule_covers_data_directory_and_symlink(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    source_ignore = Path(__file__).resolve().parents[2] / ".gitignore"
    (repository / ".gitignore").write_text(
        source_ignore.read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(
        ("git", "init", "-q"), cwd=repository, check=True,
        capture_output=True, text=True,
    )
    data = repository / "data"
    data.mkdir()
    directory_check = subprocess.run(
        ("git", "check-ignore", "data"), cwd=repository, check=False,
        capture_output=True, text=True,
    )
    assert directory_check.returncode == 0
    data.rmdir()
    persistent = tmp_path / "persistent-data"
    persistent.mkdir()
    data.symlink_to(persistent)
    symlink_check = subprocess.run(
        ("git", "check-ignore", "data"), cwd=repository, check=False,
        capture_output=True, text=True,
    )
    assert symlink_check.returncode == 0
