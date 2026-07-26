"""Prepare and atomically activate isolated CreatorFlow production releases."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


HOME = Path.home()
DEFAULT_DEVELOPMENT_ROOT = HOME / "clip-factory"
DEFAULT_PRODUCTION_ROOT = HOME / "clip-factory-production"
DEFAULT_PERSISTENT_ROOT = HOME / ".local" / "share" / "creatorflow"
DEFAULT_ENVIRONMENT_FILE = HOME / ".config" / "creatorflow" / "creatorflow.env"
DEPLOYMENT_STATE = "deployment.json"
MANAGED_UNITS = (
    "creatorflow-production.service",
    "creatorflow-production.timer",
    "creatorflow-review.service",
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class DeploymentError(RuntimeError):
    """An isolated release could not be safely prepared or activated."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


ProcessRunner = Callable[[Sequence[str], Path | None], ProcessResult]


def _absolute_path(path: Path | str) -> Path:
    """Make a launcher path absolute without dereferencing symlinks."""
    return Path(os.path.abspath(os.fspath(path)))


def run_process(command: Sequence[str], cwd: Path | None = None) -> ProcessResult:
    try:
        result = subprocess.run(
            list(command), cwd=cwd, check=False, text=True, capture_output=True,
        )
    except OSError as error:
        raise DeploymentError(f"Cannot run {command[0]!r}: {error}.") from error
    return ProcessResult(result.returncode, result.stdout, result.stderr)


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.parent / f".{link.name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


class ProductionDeployer:
    def __init__(
        self, *,
        development_root: Path = DEFAULT_DEVELOPMENT_ROOT,
        production_root: Path = DEFAULT_PRODUCTION_ROOT,
        persistent_root: Path = DEFAULT_PERSISTENT_ROOT,
        environment_file: Path = DEFAULT_ENVIRONMENT_FILE,
        unit_directory: Path | None = None,
        runner: ProcessRunner = run_process,
        python_executable: Path | None = None,
        deno_path: Path | None = None,
    ) -> None:
        self.development_root = Path(development_root).resolve()
        self.production_root = Path(production_root).resolve()
        self.persistent_root = Path(persistent_root).resolve()
        self.environment_file = Path(environment_file).resolve()
        self.unit_directory = (
            Path(unit_directory).resolve()
            if unit_directory is not None
            else (HOME / ".config" / "systemd" / "user").resolve()
        )
        self.runner = runner
        self.python_executable = _absolute_path(
            python_executable or self.development_root / ".venv" / "bin" / "python"
        )
        self.deno_path = _absolute_path(
            deno_path or HOME / ".deno" / "bin" / "deno"
        )

    @property
    def releases_root(self) -> Path:
        return self.production_root / "releases"

    @property
    def current_link(self) -> Path:
        return self.production_root / "current"

    @property
    def state_path(self) -> Path:
        return self.production_root / DEPLOYMENT_STATE

    @property
    def data_root(self) -> Path:
        return self.persistent_root / "data"

    def _run(
        self, command: Sequence[str], *, cwd: Path | None = None,
        purpose: str,
    ) -> ProcessResult:
        result = self.runner(tuple(str(value) for value in command), cwd)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise DeploymentError(
                f"{purpose} failed" + (f": {detail}" if detail else ".")
            )
        return result

    def _git(self, *arguments: str) -> str:
        return self._run(
            ("git", *arguments), cwd=self.development_root,
            purpose=f"git {' '.join(arguments)}",
        ).stdout.strip()

    def require_deployable_checkout(self) -> str:
        if self._git("status", "--porcelain"):
            raise DeploymentError("Deployment requires a clean development worktree.")
        branch = self._git("branch", "--show-current")
        if branch != "main":
            raise DeploymentError(
                f"Deployment requires development checkout branch main, found {branch!r}."
            )
        self._git("fetch", "--prune", "origin")
        commit = self._git("rev-parse", "origin/main")
        if not COMMIT_PATTERN.fullmatch(commit):
            raise DeploymentError("origin/main did not resolve to a valid Git commit.")
        local = self._git("rev-parse", "HEAD")
        if local != commit:
            raise DeploymentError(
                "Local main does not match origin/main; fast-forward main before deploying."
            )
        return commit

    def _ensure_private_environment(self) -> None:
        if not self.environment_file.is_file():
            raise DeploymentError(
                f"Private environment file is missing: {self.environment_file}."
            )
        mode = self.environment_file.stat().st_mode & 0o777
        if mode != 0o600:
            raise DeploymentError(
                f"Private environment file must have mode 0600, found {mode:04o}."
            )

    def _ensure_development_launcher(self) -> None:
        if not self.python_executable.is_file():
            raise DeploymentError(
                f"Development virtual-environment Python is missing: "
                f"{self.python_executable}."
            )
        if not os.access(self.python_executable, os.X_OK):
            raise DeploymentError(
                f"Development virtual-environment Python is not executable: "
                f"{self.python_executable}."
            )

    def _prepare_persistent_data(self) -> None:
        self.persistent_root.mkdir(parents=True, exist_ok=True)
        development_data = self.development_root / "data"
        if not self.data_root.exists():
            if development_data.is_symlink():
                resolved = development_data.resolve()
                if resolved != self.data_root:
                    raise DeploymentError(
                        f"Development data symlink targets unexpected path {resolved}."
                    )
            elif development_data.exists():
                os.replace(development_data, self.data_root)
            else:
                self.data_root.mkdir()
            if development_data.is_symlink() and not self.data_root.exists():
                self.data_root.mkdir()
        if development_data.exists() and not development_data.is_symlink():
            if development_data.resolve() != self.data_root:
                raise DeploymentError(
                    "Both development and persistent data directories exist; "
                    "refusing to merge them automatically."
                )
        if not development_data.exists():
            development_data.symlink_to(self.data_root)

    def _prepare_release(self, commit: str) -> tuple[Path, bool]:
        release = self.releases_root / commit
        marker = release / DEPLOYMENT_STATE
        if marker.is_file():
            try:
                document = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise DeploymentError(f"Release marker is unreadable: {error}.") from error
            if document.get("commit") != commit:
                raise DeploymentError("Existing release marker has the wrong commit.")
            self._validate_release(release, expected_commit=commit)
            return release, False
        if release.exists():
            raise DeploymentError(f"Incomplete release already exists: {release}.")
        self.releases_root.mkdir(parents=True, exist_ok=True)
        prepared = False
        try:
            self._run(
                ("git", "worktree", "add", "--detach", str(release), commit),
                cwd=self.development_root, purpose="release worktree creation",
            )
            prepared = True
            (release / "data").symlink_to(self.data_root)
            self._run(
                (str(self.python_executable), "-m", "venv", str(release / ".venv")),
                purpose="production virtual-environment creation",
            )
            production_python = release / ".venv" / "bin" / "python"
            self._run(
                (
                    str(production_python), "-m", "pip", "install",
                    "-r", str(release / "backend" / "requirements-transcription.txt"),
                ),
                purpose="production dependency installation",
            )
            self._validate_release(release, expected_commit=commit)
            _atomic_json(marker, {"version": 1, "commit": commit})
            return release, True
        except Exception:
            if prepared:
                self.runner(
                    ("git", "worktree", "remove", "--force", str(release)),
                    self.development_root,
                )
            if release.exists():
                shutil.rmtree(release)
            raise

    def _validate_release(
        self, release: Path, *, expected_commit: str | None = None,
    ) -> None:
        python = release / ".venv" / "bin" / "python"
        if not python.is_file():
            raise DeploymentError(f"Release Python is missing: {python}.")
        if not (release / "data").is_symlink():
            raise DeploymentError("Release data path is not a persistent-data symlink.")
        if (release / "data").resolve() != self.data_root:
            raise DeploymentError("Release data symlink targets the wrong directory.")
        commit = self._run(
            ("git", "rev-parse", "HEAD"), cwd=release,
            purpose="release commit validation",
        ).stdout.strip()
        if expected_commit is not None and commit != expected_commit:
            raise DeploymentError(
                f"Release commit {commit!r} does not match expected "
                f"origin/main commit {expected_commit!r}."
            )
        marker_path = release / DEPLOYMENT_STATE
        if marker_path.is_file():
            try:
                expected = json.loads(
                    marker_path.read_text(encoding="utf-8")
                ).get("commit")
            except (OSError, json.JSONDecodeError) as error:
                raise DeploymentError(
                    f"Release marker is unreadable: {error}."
                ) from error
            if commit != expected:
                raise DeploymentError(
                    "Release worktree commit does not match its release marker."
                )
        self._run(
            ("git", "diff", "--quiet", "HEAD", "--"), cwd=release,
            purpose="release tracked-file validation",
        )
        self._run(
            (
                str(python), "-c",
                "import yt_dlp; import yt_dlp_ejs; "
                "from importlib.resources import files; "
                "solver = files('yt_dlp_ejs.yt.solver'); "
                "assert solver.joinpath('core.min.js').is_file(); "
                "assert solver.joinpath('lib.min.js').is_file(); "
                "import backend.services.production_runner; "
                "import backend.app.review_server",
            ),
            cwd=release, purpose="release import validation",
        )
        self._run(
            (
                str(self.python_executable), "-m", "pytest",
                "-p", "no:cacheprovider",
            ),
            cwd=release, purpose="release test suite",
        )
        if not self.deno_path.is_file() or not os.access(self.deno_path, os.X_OK):
            raise DeploymentError(
                f"Required production Deno runtime is unavailable: {self.deno_path}."
            )

    def _active_services(self) -> list[str]:
        active = []
        for unit in ("creatorflow-review.service", "creatorflow-production.timer"):
            result = self.runner(("systemctl", "--user", "is-active", unit), None)
            if result.returncode == 0 and result.stdout.strip() == "active":
                active.append(unit)
        return active

    def _confirm_active(self, unit: str) -> None:
        result = self.runner(
            ("systemctl", "--user", "is-active", unit), None
        )
        if result.returncode != 0 or result.stdout.strip() != "active":
            raise DeploymentError(f"{unit} did not become active after restart.")

    def _install_units(self, release: Path, interval: str) -> list[str]:
        from backend.services.autoclip_service import AutoClipServiceManager

        verification_manager = AutoClipServiceManager(
            project_root=release,
            unit_directory=self.unit_directory,
            environment_file=self.environment_file,
            template_directory=release / "deploy" / "systemd",
            command_runner=lambda command: _service_result(self.runner(command, None)),
        )
        with tempfile.TemporaryDirectory(
            prefix="creatorflow-unit-validation-"
        ) as temporary:
            paths = []
            for name, content in verification_manager.render_units(interval).items():
                path = Path(temporary) / name
                path.write_text(content, encoding="utf-8")
                paths.append(str(path))
            self._run(
                ("systemd-analyze", "--user", "verify", *paths),
                purpose="systemd unit validation",
            )
        manager = AutoClipServiceManager(
            project_root=self.current_link,
            unit_directory=self.unit_directory,
            environment_file=self.environment_file,
            template_directory=release / "deploy" / "systemd",
            support_python_path=release / ".venv" / "bin" / "python",
            command_runner=lambda command: _service_result(self.runner(command, None)),
        )
        result = manager.install(interval)
        forbidden = str(self.development_root) + os.sep
        for unit in MANAGED_UNITS:
            content = (self.unit_directory / unit).read_text(encoding="utf-8")
            if forbidden in content:
                raise DeploymentError(
                    f"Generated unit {unit} still references development checkout."
                )
        return result["changed_units"]

    def deploy(self, *, interval: str = "30m") -> dict[str, Any]:
        commit = self.require_deployable_checkout()
        self._ensure_private_environment()
        self._ensure_development_launcher()
        self._prepare_persistent_data()
        previous = self.deployed_commit()
        release, created = self._prepare_release(commit)
        active_services = self._active_services()
        units_changed = self._install_units(release, interval)
        old_target = self.current_link.resolve() if self.current_link.exists() else None
        old_state = self.state_path.read_bytes() if self.state_path.exists() else None
        try:
            _atomic_symlink(release, self.current_link)
            _atomic_json(
                self.state_path,
                {
                    "version": 1, "deployed_commit": commit,
                    "previous_commit": previous if previous != commit else None,
                    "development_root": str(self.development_root),
                    "production_root": str(self.production_root),
                    "persistent_data_root": str(self.data_root),
                    "interval": interval,
                },
            )
            for unit in active_services:
                self._run(
                    ("systemctl", "--user", "restart", unit),
                    purpose=f"{unit} restart",
                )
                self._confirm_active(unit)
        except Exception:
            if old_target is None:
                self.current_link.unlink(missing_ok=True)
            else:
                _atomic_symlink(old_target, self.current_link)
            if old_state is None:
                self.state_path.unlink(missing_ok=True)
            else:
                _atomic_bytes(self.state_path, old_state)
            for unit in active_services:
                self.runner(("systemctl", "--user", "restart", unit), None)
            raise
        return {
            "deployed_commit": commit, "previous_commit": previous,
            "release_path": str(release), "created_release": created,
            "data_path": str(self.data_root),
            "environment_file": str(self.environment_file),
            "units_changed": units_changed,
            "restarted_units": active_services,
        }

    def deployed_commit(self) -> str | None:
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = document.get("deployed_commit")
        return value if isinstance(value, str) else None

    def status(self) -> dict[str, Any]:
        deployed = self.deployed_commit()
        result = self.runner(
            ("git", "rev-parse", "origin/main"), self.development_root
        )
        origin = result.stdout.strip() if result.returncode == 0 else None
        return {
            "deployed_commit": deployed,
            "origin_main_commit": origin,
            "production_behind_main": (
                None if not deployed or not origin else deployed != origin
            ),
            "current_release": (
                str(self.current_link.resolve())
                if self.current_link.exists() else None
            ),
        }

    def rollback(self) -> dict[str, Any]:
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeploymentError(f"Deployment state is unavailable: {error}.") from error
        previous = document.get("previous_commit")
        current = document.get("deployed_commit")
        if not isinstance(previous, str):
            raise DeploymentError("No previous release is recorded for rollback.")
        if not COMMIT_PATTERN.fullmatch(previous):
            raise DeploymentError("Recorded previous release commit is invalid.")
        release = self.releases_root / previous
        self._validate_release(release, expected_commit=previous)
        active_services = self._active_services()
        interval = document.get("interval", "30m")
        self._install_units(release, interval)
        old_target = self.current_link.resolve() if self.current_link.exists() else None
        old_state = self.state_path.read_bytes()
        try:
            _atomic_symlink(release, self.current_link)
            _atomic_json(
                self.state_path,
                {
                    **document, "deployed_commit": previous,
                    "previous_commit": current,
                },
            )
            for unit in active_services:
                self._run(
                    ("systemctl", "--user", "restart", unit),
                    purpose=f"{unit} restart",
                )
                self._confirm_active(unit)
        except Exception:
            if old_target is not None:
                _atomic_symlink(old_target, self.current_link)
            _atomic_bytes(self.state_path, old_state)
            for unit in active_services:
                self.runner(("systemctl", "--user", "restart", unit), None)
            raise
        return {
            "deployed_commit": previous, "previous_commit": current,
            "restarted_units": active_services,
        }


def _service_result(result: ProcessResult):
    from backend.services.autoclip_service import CommandResult

    return CommandResult(result.returncode, result.stdout, result.stderr)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
